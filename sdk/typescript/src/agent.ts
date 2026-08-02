import {
  Message, AgentResponse, MemoryContext, TurnResult,
  ToolDefinition, SessionInfo,
} from './types';
import {
  WorkingMemory, SemanticMemory, EpisodicMemory, ProceduralMemory,
  MetaMemory, MemoryPipeline, MemoryPipelineConfig,
} from './memory';
import { VouchstoneClient } from './client';

// ────────────────────────────────────────────────
// Agent Config
// ────────────────────────────────────────────────

export interface AgentConfig {
  name: string;
  model?: string;
  temperature?: number;
  maxTokens?: number;
  systemPrompt?: string;
  enableWorking?: boolean;
  enableSemantic?: boolean;
  enableEpisodic?: boolean;
  enableProcedural?: boolean;
  enableMeta?: boolean;
  embeddingModel?: string;
  memoryRetentionDays?: number;
  memoryConfig?: MemoryPipelineConfig;
}

// ────────────────────────────────────────────────
// Tool Registry
// ────────────────────────────────────────────────

type ToolHandler = (params: Record<string, any>) => Promise<any>;

interface RegisteredTool {
  definition: ToolDefinition;
  handler: ToolHandler;
}

// ────────────────────────────────────────────────
// Agent Base Class — full 5-layer pipeline
// ────────────────────────────────────────────────

export abstract class Agent {
  protected config: Required<AgentConfig>;
  protected memory: MemoryPipeline;
  protected tools: Map<string, RegisteredTool> = new Map();
  protected sessions: Map<string, SessionInfo> = new Map();
  protected client?: VouchstoneClient;
  private initialized = false;
  private currentSessionId: string = 'default';

  constructor(config: AgentConfig) {
    this.config = {
      name: config.name,
      model: config.model || 'gpt-4-turbo-preview',
      temperature: config.temperature ?? 0.7,
      maxTokens: config.maxTokens ?? 4096,
      systemPrompt: config.systemPrompt || '',
      enableWorking: config.enableWorking ?? true,
      enableSemantic: config.enableSemantic ?? true,
      enableEpisodic: config.enableEpisodic ?? true,
      enableProcedural: config.enableProcedural ?? false,
      enableMeta: config.enableMeta ?? false,
      embeddingModel: config.embeddingModel || 'text-embedding-3-small',
      memoryRetentionDays: config.memoryRetentionDays ?? 90,
      memoryConfig: config.memoryConfig || {},
    };

    this.memory = new MemoryPipeline(this.config.memoryConfig);
  }

  // ── Lifecycle ──

  async initialize(options?: {
    vectorDbUrl?: string;
    graphDbUrl?: string;
    redisUrl?: string;
    client?: VouchstoneClient;
  }): Promise<void> {
    this.client = options?.client;
    await this.memory.initialize();
    this.initialized = true;
  }

  // ── Tool Registration ──

  registerTool(definition: ToolDefinition, handler: ToolHandler): void {
    this.tools.set(definition.name, { definition, handler });
  }

  getAvailableTools(): ToolDefinition[] {
    return Array.from(this.tools.values()).map((t) => t.definition);
  }

  async executeTool(name: string, params: Record<string, any>): Promise<any> {
    const tool = this.tools.get(name);
    if (!tool) throw new Error(`Tool not found: ${name}`);
    return tool.handler(params);
  }

  // ── Session Management ──

  async startSession(metadata?: Record<string, any>): Promise<string> {
    const sessionId = `sess_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    const session: SessionInfo = {
      sessionId,
      agentId: this.config.name,
      startedAt: new Date().toISOString(),
      turnCount: 0,
      metadata: metadata || {},
    };
    this.sessions.set(sessionId, session);
    this.currentSessionId = sessionId;
    await this.memory.working.clear();
    return sessionId;
  }

  async endSession(sessionId?: string): Promise<SessionInfo | undefined> {
    const sid = sessionId || this.currentSessionId;
    const session = this.sessions.get(sid);
    if (session) {
      this.sessions.delete(sid);
      await this.memory.working.clear();
    }
    return session;
  }

  getSession(sessionId?: string): SessionInfo | undefined {
    return this.sessions.get(sessionId || this.currentSessionId);
  }

  // ── Main Processing Pipeline ──

  async process(message: Message, sessionId?: string): Promise<AgentResponse> {
    if (!this.initialized) throw new Error('Agent not initialized. Call initialize() first.');

    const sid = sessionId || this.currentSessionId;
    const session = this.sessions.get(sid);
    if (session) session.turnCount++;

    const startTime = Date.now();

    // 1. Prepare context from all 5 memory layers
    const context = await this.memory.prepareContext(message.content, sid);

    // 2. Store user input in working memory
    await this.memory.working.set(`input_${session?.turnCount || 0}`, {
      role: message.role || 'user',
      content: message.content,
    });

    // 3. Run the agent's implementation
    const response = await this.run(message, context);

    // 4. Process the turn through memory pipeline
    const latencyMs = Date.now() - startTime;
    const toolsUsed = (response.toolCalls || []).map((tc: any) => tc.name || tc.function?.name || 'unknown');

    await this.memory.processTurn(
      sid,
      session?.turnCount || 0,
      message.content,
      response.content,
      {
        toolsUsed,
        tokensIn: response.usage?.promptTokens || 0,
        tokensOut: response.usage?.completionTokens || 0,
        latencyMs,
      },
    );

    // 5. Store response in working memory
    await this.memory.working.set(`output_${session?.turnCount || 0}`, {
      role: 'assistant',
      content: response.content,
    });

    // 6. Report status to control plane if connected
    if (this.client) {
      this.client.registerAgentStatus(this.config.name, {
        status: 'active',
        lastTurnAt: new Date().toISOString(),
        turnCount: session?.turnCount || 0,
        latencyMs,
      });
    }

    return response;
  }

  // ── Abstract: implement this in your agent ──

  abstract run(message: Message, context: MemoryContext): Promise<AgentResponse>;

  // ── Memory Access ──

  getMemory(): MemoryPipeline {
    return this.memory;
  }

  async healthCheck() {
    return this.memory.healthCheck();
  }

  // ── Cleanup ──

  async close(): Promise<void> {
    await this.memory.close();
    this.tools.clear();
    this.sessions.clear();
  }
}
