type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class GovernanceTools {
  constructor(private api: ApiCall) {}

  definitions() {
    return [
      {
        name: 'governance_check_policy',
        description: 'Check if an action is allowed by ABAC policies. Returns allow/deny, matched policies, and any obligations (require_approval, require_dual_signoff).',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Agent performing the action' },
            action: { type: 'string', description: 'Action to check (e.g. "schema.modify_prod", "data.delete")' },
            resource: { type: 'string', description: 'Target resource' },
            context: { type: 'object', description: 'Additional context attributes for policy evaluation' },
          },
          required: ['agent_id', 'action'],
        },
      },
      {
        name: 'governance_resolve_raci',
        description: 'Resolve RACI roles for an action — who is Responsible, Accountable, Consulted, Informed.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            action: { type: 'string', description: 'Action to resolve roles for' },
            engagement_id: { type: 'string', description: 'Engagement context' },
          },
          required: ['action'],
        },
      },
      {
        name: 'governance_check_budget',
        description: 'Check budget status — remaining budget, spend rate, whether budget pause is active.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            engagement_id: { type: 'string', description: 'Engagement ID (optional, tenant-wide if omitted)' },
            agent_id: { type: 'string', description: 'Agent ID (optional)' },
          },
        },
      },
      {
        name: 'governance_audit_log',
        description: 'Query the audit log for actions, approvals, policy decisions, and governance events.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            agent_id: { type: 'string', description: 'Filter by agent' },
            action: { type: 'string', description: 'Filter by action type' },
            outcome: { type: 'string', description: 'Filter by outcome: allowed, denied, escalated' },
            time_range: { type: 'string', description: 'Time range: last_hour, last_day, last_week, last_month' },
            limit: { type: 'number', description: 'Max results (default 50)' },
          },
        },
      },
      {
        name: 'governance_list_policies',
        description: 'List all ABAC policies — scope, conditions, actions, enforcement level.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            scope: { type: 'string', description: 'Filter by scope: tenant, engagement, agent' },
          },
        },
      },
      {
        name: 'governance_vendor_risk',
        description: 'Check vendor risk status — model approvals, PII scan results, prompt injection detections.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            model_id: { type: 'string', description: 'Model ID to check approval status' },
          },
        },
      },
    ]
  }

  handlers(): Record<string, (args: any) => Promise<any>> {
    return {
      governance_check_policy: async (args) => {
        return this.api('/api/v1/policies/evaluate', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      governance_resolve_raci: async (args) => {
        return this.api('/api/v1/raci/resolve', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      governance_check_budget: async (args) => {
        const params = new URLSearchParams()
        if (args.engagement_id) params.set('engagement_id', args.engagement_id)
        if (args.agent_id) params.set('agent_id', args.agent_id)
        return this.api(`/api/v1/cost/budget-status?${params}`)
      },
      governance_audit_log: async (args) => {
        return this.api('/api/v1/audit/query', {
          method: 'POST',
          body: JSON.stringify(args),
        })
      },
      governance_list_policies: async (args) => {
        const qs = args.scope ? `?scope=${encodeURIComponent(args.scope)}` : ''
        return this.api(`/api/v1/policies${qs}`)
      },
      governance_vendor_risk: async (args) => {
        const qs = args.model_id ? `/${encodeURIComponent(args.model_id)}` : ''
        return this.api(`/api/v1/vendor-risk/models${qs}`)
      },
    }
  }
}
