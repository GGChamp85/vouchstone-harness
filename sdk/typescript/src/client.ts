import axios, { AxiosInstance, AxiosError } from 'axios';
import { AgentDefinition, AgentSpec, LedgerEntry } from './types';

export interface VouchstoneClientConfig {
  apiKey: string;
  /** Required -- there is no default. `api.vouchstone.ai` (the previous
   * fallback) does not resolve; it was a placeholder, not a real endpoint. */
  controlPlaneUrl: string;
  tenantId?: string;
  heartbeatIntervalMs?: number;
  maxRetries?: number;
  retryDelayMs?: number;
}

export class VouchstoneClient {
  private client: AxiosInstance;
  private tenantId?: string;
  private heartbeatTimer?: ReturnType<typeof setInterval>;
  private heartbeatIntervalMs: number;
  private maxRetries: number;
  private retryDelayMs: number;
  private agentStatuses: Map<string, Record<string, any>> = new Map();
  private lastLedgerTimestamp: string = '';

  constructor(config: VouchstoneClientConfig) {
    if (!config.controlPlaneUrl) {
      throw new Error(
        'VouchstoneClient: controlPlaneUrl is required -- there is no default. ' +
        'Point it at your own control plane instance.'
      );
    }
    this.tenantId = config.tenantId;
    this.heartbeatIntervalMs = config.heartbeatIntervalMs ?? 30_000;
    this.maxRetries = config.maxRetries ?? 3;
    this.retryDelayMs = config.retryDelayMs ?? 1_000;

    this.client = axios.create({
      baseURL: config.controlPlaneUrl,
      headers: {
        Authorization: `Bearer ${config.apiKey}`,
        'X-Tenant-ID': config.tenantId || '',
      },
      timeout: 30_000,
    });
  }

  // ── Agent CRUD ──

  async getAgent(agentId: string): Promise<AgentDefinition> {
    return this.withRetry(async () => {
      const { data } = await this.client.get(`/api/v1/agents/${agentId}`);
      return data;
    });
  }

  async listAgents(): Promise<AgentDefinition[]> {
    return this.withRetry(async () => {
      const { data } = await this.client.get('/api/v1/agents');
      return data.agents;
    });
  }

  // ── Agent Spec Sync ──

  async fetchAgentSpec(agentId: string): Promise<AgentSpec> {
    return this.withRetry(async () => {
      const { data } = await this.client.get(`/api/v1/agents/${agentId}/spec`);
      return data;
    });
  }

  async syncAgentSpec(agentId: string, localVersion: string): Promise<AgentSpec | null> {
    const spec = await this.fetchAgentSpec(agentId);
    if (spec.version !== localVersion) {
      return spec;
    }
    return null;
  }

  // ── Heartbeat ──

  startHeartbeat(): void {
    if (this.heartbeatTimer) return;
    this.heartbeatTimer = setInterval(() => this.sendHeartbeat(), this.heartbeatIntervalMs);
    this.sendHeartbeat();
  }

  stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = undefined;
    }
  }

  registerAgentStatus(agentId: string, status: Record<string, any>): void {
    this.agentStatuses.set(agentId, { ...status, lastUpdated: new Date().toISOString() });
  }

  private async sendHeartbeat(): Promise<void> {
    try {
      const agents: Record<string, any>[] = [];
      for (const [agentId, status] of this.agentStatuses.entries()) {
        agents.push({ agentId, ...status });
      }
      await this.client.post('/api/v1/webhooks/data-plane/heartbeat', {
        timestamp: new Date().toISOString(),
        agents,
        tenantId: this.tenantId,
      });
    } catch (err) {
      // Heartbeat failures are non-fatal — log and continue
      const message = err instanceof Error ? err.message : 'unknown';
      console.warn(`Heartbeat failed: ${message}`);
    }
  }

  // ── Ledger ──

  async submitLedgerEntry(entry: Omit<LedgerEntry, 'id' | 'timestamp'>): Promise<LedgerEntry> {
    return this.withRetry(async () => {
      const { data } = await this.client.post('/api/v1/ledger/entries', entry);
      return data;
    });
  }

  async replayLedger(agentId: string, since?: string): Promise<LedgerEntry[]> {
    const params: Record<string, string> = { agent_id: agentId };
    if (since) params.since = since;
    if (this.lastLedgerTimestamp) params.since = this.lastLedgerTimestamp;

    return this.withRetry(async () => {
      const { data } = await this.client.get('/api/v1/ledger/entries', { params });
      const entries: LedgerEntry[] = data.entries || [];
      if (entries.length > 0) {
        this.lastLedgerTimestamp = entries[entries.length - 1].timestamp;
      }
      return entries;
    });
  }

  // ── Metrics & Status ──

  async reportMetrics(metrics: Record<string, any>): Promise<void> {
    await this.withRetry(async () => {
      await this.client.post('/api/v1/webhooks/data-plane/metrics', metrics);
    });
  }

  async reportStatus(agentId: string, status: Record<string, any>): Promise<void> {
    this.registerAgentStatus(agentId, status);
    await this.withRetry(async () => {
      await this.client.post('/api/v1/webhooks/data-plane/status', {
        agent_id: agentId,
        ...status,
      });
    });
  }

  // ── KG Sync (summary-only to control plane) ──

  async syncKGSummary(summary: {
    nodeCount: number;
    edgeCount: number;
    entityTypes: Record<string, number>;
    graphHealth: number;
    lastUpdated: string;
  }): Promise<void> {
    await this.withRetry(async () => {
      await this.client.post('/api/v1/webhooks/data-plane/kg-summary', {
        tenantId: this.tenantId,
        ...summary,
      });
    });
  }

  // ── Retry Logic ──

  private async withRetry<T>(fn: () => Promise<T>): Promise<T> {
    let lastError: Error | undefined;
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      try {
        return await fn();
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
        if (err instanceof AxiosError && err.response) {
          const status = err.response.status;
          if (status === 401 || status === 403 || status === 404) throw lastError;
        }
        if (attempt < this.maxRetries) {
          await this.sleep(this.retryDelayMs * Math.pow(2, attempt));
        }
      }
    }
    throw lastError!;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ── Cleanup ──

  async close(): Promise<void> {
    this.stopHeartbeat();
    this.agentStatuses.clear();
  }
}
