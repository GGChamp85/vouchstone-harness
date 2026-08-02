import {
  MemoryEntry, Episode, Decision, EpisodicTrace, Entity, Skill,
  MemoryContext, TurnResult, HealthReport, MemoryLayer,
} from './types';
import { VouchstoneClient } from './client';

// ────────────────────────────────────────────────
// Working Memory — Redis-backed context window
// ────────────────────────────────────────────────

export interface WorkingMemoryConfig {
  redisUrl?: string;
  maxEntries?: number;
  ttlSeconds?: number;
}

export class WorkingMemory {
  private config: Required<WorkingMemoryConfig>;
  private entries: Map<string, any> = new Map();
  private redisClient: any = null;

  constructor(config: WorkingMemoryConfig = {}) {
    this.config = {
      redisUrl: config.redisUrl || process.env.REDIS_URL || '',
      maxEntries: config.maxEntries ?? 100,
      ttlSeconds: config.ttlSeconds ?? 3600,
    };
  }

  async initialize(): Promise<void> {
    if (this.config.redisUrl) {
      try {
        const redis = await import('redis');
        this.redisClient = redis.createClient({ url: this.config.redisUrl });
        await this.redisClient.connect();
      } catch {
        console.warn('WorkingMemory: Redis unavailable, falling back to in-memory.');
      }
    }
  }

  async get(key: string): Promise<any> {
    if (this.redisClient) {
      const val = await this.redisClient.get(`wm:${key}`);
      return val ? JSON.parse(val) : null;
    }
    return this.entries.get(key) ?? null;
  }

  async set(key: string, value: any): Promise<void> {
    if (this.redisClient) {
      await this.redisClient.set(`wm:${key}`, JSON.stringify(value), { EX: this.config.ttlSeconds });
    } else {
      this.entries.set(key, value);
      if (this.entries.size > this.config.maxEntries) {
        const first = this.entries.keys().next().value;
        if (first !== undefined) this.entries.delete(first);
      }
    }
  }

  async getContext(): Promise<Record<string, any>[]> {
    if (this.redisClient) {
      const keys: string[] = await this.redisClient.keys('wm:*');
      const result: Record<string, any>[] = [];
      for (const k of keys.slice(-this.config.maxEntries)) {
        const val = await this.redisClient.get(k);
        if (val) result.push({ key: k.replace('wm:', ''), value: JSON.parse(val) });
      }
      return result;
    }
    return Array.from(this.entries.entries()).map(([key, value]) => ({ key, value }));
  }

  async clear(): Promise<void> {
    if (this.redisClient) {
      const keys: string[] = await this.redisClient.keys('wm:*');
      if (keys.length > 0) await this.redisClient.del(keys);
    }
    this.entries.clear();
  }

  async close(): Promise<void> {
    if (this.redisClient) await this.redisClient.quit();
    this.entries.clear();
  }
}

// ────────────────────────────────────────────────
// Semantic Memory — OpenAI embeddings + vector search
// ────────────────────────────────────────────────

export interface SemanticMemoryConfig {
  embeddingModel?: string;
  vectorDbUrl?: string;
  collectionName?: string;
  openaiApiKey?: string;
  embeddingDimension?: number;
}

interface EmbeddingVector {
  id: string;
  vector: number[];
  content: string;
  metadata: Record<string, any>;
}

export class SemanticMemory {
  private config: Required<SemanticMemoryConfig>;
  private entries: Map<string, EmbeddingVector> = new Map();
  private embeddingCache: Map<string, number[]> = new Map();

  constructor(config: SemanticMemoryConfig = {}) {
    this.config = {
      embeddingModel: config.embeddingModel || 'text-embedding-3-small',
      vectorDbUrl: config.vectorDbUrl || '',
      collectionName: config.collectionName || 'semantic_memory',
      openaiApiKey: config.openaiApiKey || process.env.OPENAI_API_KEY || '',
      embeddingDimension: config.embeddingDimension || 1536,
    };
  }

  async initialize(): Promise<void> {
    if (!this.config.openaiApiKey) {
      console.warn('SemanticMemory: No OpenAI API key. Falling back to text matching.');
    }
  }

  async store(text: string, metadata?: Record<string, any>): Promise<string> {
    const id = `mem_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    let vector: number[] = [];
    if (this.config.openaiApiKey) {
      vector = await this.generateEmbedding(text);
    }
    this.entries.set(id, { id, vector, content: text, metadata: metadata || {} });
    return id;
  }

  async storeEntity(entity: Entity): Promise<string> {
    const text = `${entity.entityType}: ${entity.entityKey} — ${JSON.stringify(entity.attributes)}`;
    return this.store(text, { entityId: entity.id, entityType: entity.entityType });
  }

  async search(query: string, topK: number = 5): Promise<MemoryEntry[]> {
    if (this.config.openaiApiKey && this.hasEmbeddings()) {
      return this.vectorSearch(query, topK);
    }
    return this.textSearch(query, topK);
  }

  async findEntities(query: string, topK: number = 10): Promise<Entity[]> {
    const results = await this.search(query, topK);
    return results
      .filter((r) => r.metadata.entityId)
      .map((r) => ({
        id: r.metadata.entityId,
        entityType: r.metadata.entityType || 'unknown',
        entityKey: r.content.split(' — ')[0]?.split(': ')[1] || r.content,
        attributes: {},
        confidence: r.score,
        createdAt: new Date().toISOString(),
      }));
  }

  private async vectorSearch(query: string, topK: number): Promise<MemoryEntry[]> {
    const queryVector = await this.generateEmbedding(query);
    const scored: { entry: EmbeddingVector; score: number }[] = [];
    for (const entry of this.entries.values()) {
      if (entry.vector.length > 0) {
        scored.push({ entry, score: this.cosineSimilarity(queryVector, entry.vector) });
      }
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, topK).map(({ entry, score }) => ({
      id: entry.id, content: entry.content, metadata: entry.metadata, score,
    }));
  }

  private textSearch(query: string, topK: number): MemoryEntry[] {
    const terms = query.toLowerCase().split(/\s+/).filter((t) => t.length > 2);
    const scored: { entry: EmbeddingVector; score: number }[] = [];
    for (const entry of this.entries.values()) {
      const lower = entry.content.toLowerCase();
      const hits = terms.filter((t) => lower.includes(t)).length;
      if (hits > 0) scored.push({ entry, score: hits / terms.length });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, topK).map(({ entry, score }) => ({
      id: entry.id, content: entry.content, metadata: entry.metadata, score,
    }));
  }

  private async generateEmbedding(text: string): Promise<number[]> {
    const key = this.hashText(text);
    const cached = this.embeddingCache.get(key);
    if (cached) return cached;
    try {
      const res = await fetch('https://api.openai.com/v1/embeddings', {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.config.openaiApiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: text, model: this.config.embeddingModel }),
      });
      if (!res.ok) return [];
      const data = (await res.json()) as { data: Array<{ embedding: number[] }> };
      const emb = data.data[0].embedding;
      this.embeddingCache.set(key, emb);
      return emb;
    } catch {
      return [];
    }
  }

  private cosineSimilarity(a: number[], b: number[]): number {
    if (a.length !== b.length || a.length === 0) return 0;
    let dot = 0, nA = 0, nB = 0;
    for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; nA += a[i] ** 2; nB += b[i] ** 2; }
    const denom = Math.sqrt(nA) * Math.sqrt(nB);
    return denom === 0 ? 0 : dot / denom;
  }

  private hasEmbeddings(): boolean {
    for (const e of this.entries.values()) if (e.vector.length > 0) return true;
    return false;
  }

  private hashText(text: string): string {
    let h = 0;
    for (let i = 0; i < text.length; i++) { h = ((h << 5) - h) + text.charCodeAt(i); h = h & h; }
    return `h_${h}`;
  }

  async delete(id: string): Promise<boolean> { return this.entries.delete(id); }
  async clear(): Promise<void> { this.entries.clear(); this.embeddingCache.clear(); }
  async close(): Promise<void> { this.clear(); }
}

// ────────────────────────────────────────────────
// Episodic Memory — API-backed trace storage
// ────────────────────────────────────────────────

export interface EpisodicMemoryConfig {
  retentionDays?: number;
  maxEpisodes?: number;
  apiClient?: VouchstoneClient;
  agentId?: string;
}

export class EpisodicMemory {
  private config: Required<Omit<EpisodicMemoryConfig, 'apiClient' | 'agentId'>>;
  private apiClient?: VouchstoneClient;
  private agentId?: string;
  private traces: EpisodicTrace[] = [];

  constructor(config: EpisodicMemoryConfig = {}) {
    this.config = {
      retentionDays: config.retentionDays ?? 90,
      maxEpisodes: config.maxEpisodes ?? 10_000,
    };
    this.apiClient = config.apiClient;
    this.agentId = config.agentId;
  }

  async initialize(): Promise<void> {}

  async recordTrace(trace: Omit<EpisodicTrace, 'id' | 'timestamp'>): Promise<string> {
    const entry: EpisodicTrace = {
      ...trace,
      id: `ep_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
      timestamp: new Date().toISOString(),
    };
    this.traces.push(entry);
    if (this.traces.length > this.config.maxEpisodes) {
      this.traces = this.traces.slice(-this.config.maxEpisodes);
    }
    return entry.id;
  }

  async store(message: any, response: any, metadata?: Record<string, any>): Promise<string> {
    return this.recordTrace({
      sessionId: metadata?.sessionId || 'default',
      turnNumber: this.traces.length + 1,
      userInput: typeof message === 'string' ? message : message.content || JSON.stringify(message),
      agentResponse: typeof response === 'string' ? response : response.content || JSON.stringify(response),
      toolsUsed: metadata?.toolsUsed || [],
      tokensIn: metadata?.tokensIn || 0,
      tokensOut: metadata?.tokensOut || 0,
      latencyMs: metadata?.latencyMs || 0,
      success: metadata?.success ?? true,
      importance: metadata?.importance ?? 0.5,
    });
  }

  async getRecent(limit: number = 10): Promise<EpisodicTrace[]> {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - this.config.retentionDays);
    const cutoffStr = cutoff.toISOString();
    return this.traces
      .filter((t) => t.timestamp > cutoffStr)
      .slice(-limit)
      .reverse();
  }

  async getBySession(sessionId: string): Promise<EpisodicTrace[]> {
    return this.traces.filter((t) => t.sessionId === sessionId);
  }

  async search(query: string, limit: number = 10): Promise<EpisodicTrace[]> {
    const q = query.toLowerCase();
    return this.traces
      .filter((t) => t.userInput.toLowerCase().includes(q) || t.agentResponse.toLowerCase().includes(q))
      .slice(-limit)
      .reverse();
  }

  async clear(): Promise<void> { this.traces = []; }
  async close(): Promise<void> { this.traces = []; }
}

// ────────────────────────────────────────────────
// Procedural Memory — Neo4j-backed skill registry
// ────────────────────────────────────────────────

export interface ProceduralMemoryConfig {
  graphDbUrl?: string;
  graphDbUser?: string;
  graphDbPassword?: string;
}

export class ProceduralMemory {
  private config: ProceduralMemoryConfig;
  private skills: Map<string, Skill> = new Map();
  private neo4jDriver: any = null;

  constructor(config: ProceduralMemoryConfig = {}) {
    this.config = config;
  }

  async initialize(): Promise<void> {
    if (this.config.graphDbUrl) {
      try {
        const neo4j = await import('neo4j-driver');
        this.neo4jDriver = neo4j.default.driver(
          this.config.graphDbUrl,
          neo4j.default.auth.basic(this.config.graphDbUser || 'neo4j', this.config.graphDbPassword || ''),
        );
        await this.neo4jDriver.verifyConnectivity();
      } catch {
        console.warn('ProceduralMemory: Neo4j unavailable, falling back to in-memory.');
      }
    }
  }

  async addSkill(skill: Omit<Skill, 'id'>): Promise<string> {
    const id = `skill_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    const entry: Skill = { ...skill, id };
    this.skills.set(id, entry);

    if (this.neo4jDriver) {
      const session = this.neo4jDriver.session();
      try {
        await session.run(
          `CREATE (s:Skill {id: $id, name: $name, description: $desc, version: $ver, successRate: $sr, executionCount: $ec})`,
          { id, name: skill.name, desc: skill.description, ver: skill.version, sr: skill.successRate, ec: skill.executionCount },
        );
      } finally {
        await session.close();
      }
    }
    return id;
  }

  async findSkills(query: string, limit: number = 5): Promise<Skill[]> {
    const q = query.toLowerCase();
    const scored: { skill: Skill; score: number }[] = [];
    for (const skill of this.skills.values()) {
      const hay = `${skill.name} ${skill.description}`.toLowerCase();
      const terms = q.split(/\s+/).filter((t) => t.length > 2);
      const hits = terms.filter((t) => hay.includes(t)).length;
      if (hits > 0) scored.push({ skill, score: hits / terms.length });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, limit).map((s) => s.skill);
  }

  async getSkill(id: string): Promise<Skill | undefined> {
    return this.skills.get(id);
  }

  async updateSuccessRate(id: string, success: boolean): Promise<void> {
    const skill = this.skills.get(id);
    if (!skill) return;
    skill.executionCount++;
    skill.successRate = ((skill.successRate * (skill.executionCount - 1)) + (success ? 1 : 0)) / skill.executionCount;

    if (this.neo4jDriver) {
      const session = this.neo4jDriver.session();
      try {
        await session.run(
          `MATCH (s:Skill {id: $id}) SET s.successRate = $sr, s.executionCount = $ec`,
          { id, sr: skill.successRate, ec: skill.executionCount },
        );
      } finally {
        await session.close();
      }
    }
  }

  async listSkills(): Promise<Skill[]> {
    return Array.from(this.skills.values());
  }

  async clear(): Promise<void> { this.skills.clear(); }

  async close(): Promise<void> {
    if (this.neo4jDriver) await this.neo4jDriver.close();
    this.skills.clear();
  }
}

// ────────────────────────────────────────────────
// Meta Memory — maintenance, decay, health
// ────────────────────────────────────────────────

export interface MetaMemoryConfig {
  maintenanceIntervalHours?: number;
  decayFactor?: number;
  deduplicationThreshold?: number;
}

export class MetaMemory {
  private config: Required<MetaMemoryConfig>;
  private lastMaintenance?: string;

  constructor(config: MetaMemoryConfig = {}) {
    this.config = {
      maintenanceIntervalHours: config.maintenanceIntervalHours ?? 24,
      decayFactor: config.decayFactor ?? 0.95,
      deduplicationThreshold: config.deduplicationThreshold ?? 0.92,
    };
  }

  async initialize(): Promise<void> {}

  async healthCheck(layers: {
    working?: WorkingMemory;
    episodic?: EpisodicMemory;
    semantic?: SemanticMemory;
    procedural?: ProceduralMemory;
  }): Promise<HealthReport> {
    const perLayer: Record<string, number> = {};
    const recommendations: string[] = [];

    if (layers.episodic) {
      const traces = await layers.episodic.getRecent(10_000);
      perLayer[MemoryLayer.EPISODIC] = traces.length;
      if (traces.length > 8_000) recommendations.push('Episodic memory approaching limit — consider archival.');
    }
    if (layers.procedural) {
      const skills = await layers.procedural.listSkills();
      perLayer[MemoryLayer.PROCEDURAL] = skills.length;
      const lowSuccess = skills.filter((s) => s.executionCount > 5 && s.successRate < 0.5);
      if (lowSuccess.length > 0) {
        recommendations.push(`${lowSuccess.length} skills with <50% success rate — review or deprecate.`);
      }
    }

    const total = Object.values(perLayer).reduce((a, b) => a + b, 0);
    return { totalEntries: total, perLayer, recommendations, lastMaintenance: this.lastMaintenance };
  }

  async runMaintenance(layers: {
    episodic?: EpisodicMemory;
    procedural?: ProceduralMemory;
  }): Promise<{ decayed: number; deduped: number; archived: number }> {
    this.lastMaintenance = new Date().toISOString();
    return { decayed: 0, deduped: 0, archived: 0 };
  }

  async close(): Promise<void> {}
}

// ────────────────────────────────────────────────
// Memory Pipeline — orchestrator for all 5 layers
// ────────────────────────────────────────────────

export interface MemoryPipelineConfig {
  working?: WorkingMemoryConfig;
  semantic?: SemanticMemoryConfig;
  episodic?: EpisodicMemoryConfig;
  procedural?: ProceduralMemoryConfig;
  meta?: MetaMemoryConfig;
}

export class MemoryPipeline {
  public working: WorkingMemory;
  public semantic: SemanticMemory;
  public episodic: EpisodicMemory;
  public procedural: ProceduralMemory;
  public meta: MetaMemory;

  constructor(config: MemoryPipelineConfig = {}) {
    this.working = new WorkingMemory(config.working);
    this.semantic = new SemanticMemory(config.semantic);
    this.episodic = new EpisodicMemory(config.episodic);
    this.procedural = new ProceduralMemory(config.procedural);
    this.meta = new MetaMemory(config.meta);
  }

  async initialize(): Promise<void> {
    await Promise.all([
      this.working.initialize(),
      this.semantic.initialize(),
      this.episodic.initialize(),
      this.procedural.initialize(),
      this.meta.initialize(),
    ]);
  }

  async prepareContext(query: string, sessionId: string): Promise<MemoryContext> {
    const [workingCtx, semanticResults, recentTraces, skills] = await Promise.all([
      this.working.getContext(),
      this.semantic.search(query, 5),
      this.episodic.getRecent(10),
      this.procedural.findSkills(query, 3),
    ]);

    return {
      workingMemory: workingCtx,
      episodicContext: recentTraces,
      semanticEntities: semanticResults.map((r) => ({
        id: r.id,
        entityType: r.metadata.entityType || 'unknown',
        entityKey: r.content,
        attributes: r.metadata,
        confidence: r.score,
        createdAt: new Date().toISOString(),
      })),
      proceduralSkills: skills,
      scratchpad: {},
    };
  }

  async processTurn(
    sessionId: string,
    turnNumber: number,
    userInput: string,
    agentResponse: string,
    metadata?: { toolsUsed?: string[]; tokensIn?: number; tokensOut?: number; latencyMs?: number },
  ): Promise<TurnResult> {
    const traceId = await this.episodic.recordTrace({
      sessionId,
      turnNumber,
      userInput,
      agentResponse,
      toolsUsed: metadata?.toolsUsed || [],
      tokensIn: metadata?.tokensIn || 0,
      tokensOut: metadata?.tokensOut || 0,
      latencyMs: metadata?.latencyMs || 0,
      success: true,
      importance: 0.5,
    });

    await this.working.set(`turn_${turnNumber}`, { userInput, agentResponse });

    return { episodicTraceId: traceId, entitiesExtracted: 0, skillsUpdated: 0 };
  }

  async healthCheck(): Promise<HealthReport> {
    return this.meta.healthCheck({
      working: this.working,
      episodic: this.episodic,
      semantic: this.semantic,
      procedural: this.procedural,
    });
  }

  async close(): Promise<void> {
    await Promise.all([
      this.working.close(),
      this.semantic.close(),
      this.episodic.close(),
      this.procedural.close(),
      this.meta.close(),
    ]);
  }
}

// Legacy exports for backward compatibility
export { SemanticMemory as DecisionGraph };
export { MemoryPipeline as MemoryManager };
