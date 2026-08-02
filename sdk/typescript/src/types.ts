export interface Message {
  content: string;
  role?: 'user' | 'assistant' | 'system';
  metadata?: Record<string, any>;
  timestamp?: string;
}

export interface Decision {
  id: string;
  question: string;
  answer: string;
  confidence: number;
  reasoning?: Record<string, any>[];
  metadata?: Record<string, any>;
  outcome?: string;
  timestamp?: string;
}

export interface AgentResponse {
  content: string;
  decisions?: Decision[];
  toolCalls?: Record<string, any>[];
  metadata?: Record<string, any>;
  usage?: { promptTokens?: number; completionTokens?: number };
}

export interface MemoryEntry {
  id: string;
  content: string;
  metadata: Record<string, any>;
  score: number;
}

export interface Episode {
  id: string;
  timestamp: string;
  message: string;
  response: string;
  metadata: Record<string, any>;
}

export interface EpisodicTrace {
  id: string;
  sessionId: string;
  turnNumber: number;
  userInput: string;
  agentResponse: string;
  toolsUsed: string[];
  tokensIn: number;
  tokensOut: number;
  latencyMs: number;
  success: boolean;
  importance: number;
  timestamp: string;
}

export interface Entity {
  id: string;
  entityType: string;
  entityKey: string;
  attributes: Record<string, any>;
  confidence: number;
  sourceTraceId?: string;
  createdAt: string;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  steps: string[];
  toolsRequired: string[];
  prerequisites: string[];
  successRate: number;
  avgLatencyMs: number;
  executionCount: number;
  version: number;
}

export interface MemoryContext {
  workingMemory: Record<string, any>[];
  episodicContext: EpisodicTrace[];
  semanticEntities: Entity[];
  proceduralSkills: Skill[];
  scratchpad: Record<string, any>;
}

export interface TurnResult {
  episodicTraceId: string;
  entitiesExtracted: number;
  skillsUpdated: number;
}

export interface HealthReport {
  totalEntries: number;
  perLayer: Record<string, number>;
  recommendations: string[];
  lastMaintenance?: string;
}

export interface AgentDefinition {
  id: string;
  name: string;
  slug: string;
  agentType: string;
  status: string;
  memoryConfig: MemoryConfig;
  configSchema: Record<string, any>;
  codeS3Key?: string;
  description?: string;
  tags?: string[];
}

export interface MemoryConfig {
  semantic?: { enabled: boolean; embeddingModel?: string; indexName?: string };
  episodic?: { enabled: boolean; retentionDays?: number };
  procedural?: { enabled: boolean };
  working?: { enabled: boolean; maxEntries?: number };
  meta?: { enabled: boolean; maintenanceIntervalHours?: number };
}

export interface LedgerEntry {
  id: string;
  agentId: string;
  action: string;
  input: Record<string, any>;
  output: Record<string, any>;
  status: 'pending' | 'committed' | 'rolled_back';
  hash?: string;
  parentHash?: string;
  timestamp: string;
}

export interface AgentSpec {
  agentId: string;
  name: string;
  version: string;
  tools: ToolDefinition[];
  memoryConfig: MemoryConfig;
  systemPrompt: string;
  maxTokens: number;
  temperature: number;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, any>;
  handler?: string;
}

export interface SessionInfo {
  sessionId: string;
  agentId: string;
  startedAt: string;
  turnCount: number;
  metadata: Record<string, any>;
}

export enum AgentStatus {
  DRAFT = 'draft',
  ACTIVE = 'active',
  PAUSED = 'paused',
  ARCHIVED = 'archived',
}

export enum MemoryLayer {
  WORKING = 'working',
  EPISODIC = 'episodic',
  SEMANTIC = 'semantic',
  PROCEDURAL = 'procedural',
  META = 'meta',
}
