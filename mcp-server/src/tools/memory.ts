type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class MemoryTools {
  constructor(private api: ApiCall) {}

  definitions() {
    return [
      {
        name: 'memory_query_episodic',
        description: 'Query episodic memory — what happened in past sessions. Returns traces of agent actions, decisions, and outcomes.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID (optional, all agents if omitted)' },
            query: { type: 'string', description: 'Natural language query, e.g. "When was the last schema migration?" or "What parity issues were found?"' },
            time_range: { type: 'string', description: 'Time range filter: last_hour, last_day, last_week, last_month, all' },
            limit: { type: 'number', description: 'Max results (default 20)' },
          },
          required: ['query'],
        },
      },
      {
        name: 'memory_query_semantic',
        description: 'Query semantic memory — entity knowledge extracted from episodes. Search by natural language or vector similarity.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            query: { type: 'string', description: 'Search query' },
            entity_type: { type: 'string', description: 'Filter by entity type' },
            limit: { type: 'number', description: 'Max results (default 20)' },
          },
          required: ['query'],
        },
      },
      {
        name: 'memory_query_procedural',
        description: 'Query procedural memory — learned skills and proven procedures. Returns versioned skill definitions with confidence scores.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            skill_name: { type: 'string', description: 'Skill name to look up (or partial match)' },
            min_confidence: { type: 'number', description: 'Minimum confidence threshold (0-1, default 0.7)' },
            agent_id: { type: 'string', description: 'Filter by agent' },
          },
        },
      },
      {
        name: 'memory_get_context',
        description: 'Get the full memory context for a given query — pulls from all 5 memory layers (working, episodic, semantic, procedural, meta) and returns a unified context.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            query: { type: 'string', description: 'The task or question to build context for' },
            agent_id: { type: 'string', description: 'Agent perspective (optional)' },
          },
          required: ['query'],
        },
      },
      {
        name: 'memory_stats',
        description: 'Get memory layer statistics — entry counts, sizes, last write times, decay status for each layer.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID (optional, aggregate if omitted)' },
          },
        },
      },
    ]
  }

  handlers(): Record<string, (args: any) => Promise<any>> {
    return {
      memory_query_episodic: async (args) => {
        return this.api('/api/v1/memory-stores/episodic/query', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      memory_query_semantic: async (args) => {
        return this.api('/api/v1/memory-stores/semantic/query', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      memory_query_procedural: async (args) => {
        const params = new URLSearchParams()
        if (args.skill_name) params.set('name', args.skill_name)
        if (args.min_confidence) params.set('min_confidence', String(args.min_confidence))
        if (args.agent_id) params.set('agent_id', args.agent_id)
        return this.api(`/api/v1/memory-stores/procedural?${params}`)
      },
      memory_get_context: async (args) => {
        return this.api('/api/v1/memory-stores/context', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      memory_stats: async (args) => {
        const qs = args.agent_id ? `?agent_id=${encodeURIComponent(args.agent_id)}` : ''
        return this.api(`/api/v1/memory-stores/stats${qs}`)
      },
    }
  }
}
