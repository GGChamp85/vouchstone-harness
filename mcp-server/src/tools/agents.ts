type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class AgentTools {
  constructor(private api: ApiCall) {}

  definitions() {
    return [
      {
        name: 'agent_list',
        description: 'List all agents in the current tenant. Returns name, role, tier, status, and skill summary for each agent.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            status: { type: 'string', description: 'Filter by status: active, idle, paused, error' },
            tier: { type: 'number', description: 'Filter by autonomy tier (1-3)' },
          },
        },
      },
      {
        name: 'agent_get',
        description: 'Get detailed information about a specific agent — configuration, skills, recent activity, memory stats, guardrail status.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name (e.g. "atlas", "data-engineer")' },
          },
          required: ['agent_id'],
        },
      },
      {
        name: 'agent_execute',
        description: 'Execute an action on an agent. The agent will use its skills, tools, and memory to complete the task. Returns the result and execution trace.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
            action: { type: 'string', description: 'Action to execute (must be in agent\'s allowed actions)' },
            params: { type: 'object', description: 'Action parameters' },
            wait: { type: 'boolean', description: 'Wait for completion (default true). Set false for async execution.' },
          },
          required: ['agent_id', 'action'],
        },
      },
      {
        name: 'agent_ask',
        description: 'Ask an agent a question using its domain expertise and knowledge graph context. The agent retrieves relevant memory and KG context before responding.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
            question: { type: 'string', description: 'Question to ask the agent' },
          },
          required: ['agent_id', 'question'],
        },
      },
      {
        name: 'agent_skills',
        description: 'List an agent\'s skills with descriptions, parameters, and confidence scores from procedural memory.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
          },
          required: ['agent_id'],
        },
      },
      {
        name: 'agent_traces',
        description: 'Get recent execution traces for an agent — what it did, what tools it called, what decisions it made, and the outcome.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
            limit: { type: 'number', description: 'Number of recent traces (default 10)' },
            action_filter: { type: 'string', description: 'Filter by action type' },
          },
          required: ['agent_id'],
        },
      },
      {
        name: 'agent_pause',
        description: 'Pause an agent. It will stop processing new tasks but complete any in-progress work.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
            reason: { type: 'string', description: 'Reason for pausing' },
          },
          required: ['agent_id'],
        },
      },
      {
        name: 'agent_resume',
        description: 'Resume a paused agent.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent ID or name' },
          },
          required: ['agent_id'],
        },
      },
    ]
  }

  handlers(): Record<string, (args: any) => Promise<any>> {
    return {
      agent_list: async (args) => {
        const params = new URLSearchParams()
        if (args.status) params.set('status', args.status)
        if (args.tier) params.set('tier', String(args.tier))
        const qs = params.toString()
        return this.api(`/api/v1/agents${qs ? '?' + qs : ''}`)
      },
      agent_get: async (args) => this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}`),
      agent_execute: async (args) => {
        return this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/execute`, {
          method: 'POST',
          body: JSON.stringify({ action: args.action, params: args.params || {}, wait: args.wait !== false }),
        })
      },
      agent_ask: async (args) => {
        return this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/ask`, {
          method: 'POST',
          body: JSON.stringify({ question: args.question }),
        })
      },
      agent_skills: async (args) => this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/skills`),
      agent_traces: async (args) => {
        const params = new URLSearchParams({ limit: String(args.limit || 10) })
        if (args.action_filter) params.set('action', args.action_filter)
        return this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/traces?${params}`)
      },
      agent_pause: async (args) => {
        return this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/pause`, {
          method: 'POST',
          body: JSON.stringify({ reason: args.reason || '' }),
        })
      },
      agent_resume: async (args) => {
        return this.api(`/api/v1/agents/${encodeURIComponent(args.agent_id)}/resume`, {
          method: 'POST',
        })
      },
    }
  }
}
