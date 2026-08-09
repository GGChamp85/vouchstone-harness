import { randomUUID } from 'node:crypto'

type ApiCall = (path: string, options?: RequestInit) => Promise<any>

// Every route below was checked against the real routers, not assumed
// from naming convention (control-plane/backend/app/api/v1/endpoints/
// memory_stores.py and memory_pipeline.py). Every one of them requires
// tenant_id as an explicit query param -- the X-Tenant-ID header the
// server already sends isn't consumed by these FastAPI routes.
export class MemoryTools {
  constructor(private api: ApiCall, private tenantId: string) {}

  definitions() {
    return [
      {
        name: 'memory_query_episodic',
        description: 'Full-text search over episodic memory -- what happened in past sessions. Returns entries whose key/content match the query.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            query: { type: 'string', description: 'Search text (substring match against entry key/content)' },
            agent_id: { type: 'string', description: 'Agent ID (optional, searches all agents in the tenant if omitted)' },
            limit: { type: 'number', description: 'Max results (default 20)' },
          },
          required: ['query'],
        },
      },
      {
        name: 'memory_query_semantic',
        description: 'Query semantic memory -- entity knowledge extracted from episodes -- for one agent, via vector similarity where embeddings are configured (falls back to text search otherwise).',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID (required -- semantic search is scoped per agent)' },
            query: { type: 'string', description: 'Search query' },
            entity_type: { type: 'string', description: 'Filter results by entity type (applied client-side after retrieval)' },
            limit: { type: 'number', description: 'Max results (default 10)' },
          },
          required: ['agent_id', 'query'],
        },
      },
      {
        name: 'memory_query_procedural',
        description: 'List an agent\'s procedural memory -- learned skills with version, success rate, and execution count. Optionally filtered by name/success-rate threshold.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID (required -- procedural memory is scoped per agent)' },
            skill_name: { type: 'string', description: 'Skill name to look up (substring match, applied client-side)' },
            min_confidence: { type: 'number', description: 'Minimum success_rate threshold (0-1, applied client-side)' },
          },
          required: ['agent_id'],
        },
      },
      {
        name: 'memory_get_context',
        description: 'Get the full memory context for a query -- pulls from working, episodic, semantic, and procedural memory in one call, exactly like the runtime does before a real agent turn. Issues a fresh, isolated scratch session_id per call, so this never reads or writes into a real agent session.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID' },
            query: { type: 'string', description: 'The task or question to build context for' },
          },
          required: ['agent_id', 'query'],
        },
      },
      {
        name: 'memory_stats',
        description: 'Snapshot of an agent\'s memory -- episodic/semantic/procedural entry counts, sample entities and skills, and the tenant\'s memory health report (decay/dedup/compression status).',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID (required)' },
          },
          required: ['agent_id'],
        },
      },
    ]
  }

  handlers(): Record<string, (args: any) => Promise<any>> {
    return {
      memory_query_episodic: async (args) => {
        return this.api(`/api/v1/memory-stores/entries/search?tenant_id=${this.tenantId}`, {
          method: 'POST',
          body: JSON.stringify({
            query: args.query,
            memory_type: 'episodic',
            source: args.agent_id ? `agent:${args.agent_id}` : undefined,
            limit: args.limit || 20,
          }),
        })
      },
      memory_query_semantic: async (args) => {
        const params = new URLSearchParams({
          tenant_id: this.tenantId,
          agent_id: args.agent_id,
          query: args.query,
          max_results: String(args.limit || 10),
        })
        // entities/search takes query/agent_id/max_results as plain query
        // params (no request body) -- entity_type isn't exposed at the
        // route level even though the underlying service supports it, so
        // it's filtered client-side here instead of silently dropped.
        const results = await this.api(`/api/v1/memory-pipeline/entities/search?${params}`, { method: 'POST' })
        if (args.entity_type && Array.isArray(results)) {
          return results.filter((r: any) => r.entity_type === args.entity_type)
        }
        return results
      },
      memory_query_procedural: async (args) => {
        const skills = await this.api(`/api/v1/memory-pipeline/skills/${encodeURIComponent(args.agent_id)}?tenant_id=${this.tenantId}`)
        let filtered = Array.isArray(skills) ? skills : []
        if (args.skill_name) {
          const needle = args.skill_name.toLowerCase()
          filtered = filtered.filter((s: any) => (s.skill_name || '').toLowerCase().includes(needle))
        }
        if (args.min_confidence != null) {
          filtered = filtered.filter((s: any) => (s.success_rate || 0) >= args.min_confidence)
        }
        return filtered
      },
      memory_get_context: async (args) => {
        return this.api(`/api/v1/memory-pipeline/prepare-context?tenant_id=${this.tenantId}`, {
          method: 'POST',
          body: JSON.stringify({
            agent_id: args.agent_id,
            session_id: `mcp-scratch-${randomUUID()}`,
            user_input: args.query,
          }),
        })
      },
      memory_stats: async (args) => {
        return this.api(`/api/v1/memory-pipeline/snapshot/${encodeURIComponent(args.agent_id)}?tenant_id=${this.tenantId}`)
      },
    }
  }
}
