type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class KnowledgeGraphTools {
  constructor(private api: ApiCall) {}

  definitions() {
    return [
      {
        name: 'kg_query',
        description: 'Query the Knowledge Graph using natural language. Returns entities and relationships matching the query.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            query: { type: 'string', description: 'Natural language query, e.g. "What systems process PII?" or "Show all compliance nodes"' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
            kind: { type: 'string', description: 'Filter by node kind: person, system, data, convention, compliance, decision' },
            limit: { type: 'number', description: 'Max results to return (default 20)' },
          },
          required: ['query', 'tenant_id'],
        },
      },
      {
        name: 'kg_get_node',
        description: 'Get a specific node from the Knowledge Graph by ID. Returns full attributes, confidence, and status.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            node_id: { type: 'string', description: 'Node UUID' },
          },
          required: ['node_id'],
        },
      },
      {
        name: 'kg_list_nodes',
        description: 'List Knowledge Graph nodes with filters by kind, status, and confidence.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            kind: { type: 'string', description: 'Node kind: person, system, data, convention, compliance, decision' },
            status: { type: 'string', description: 'Node status: draft, promoted, rejected (default: promoted)' },
            limit: { type: 'number', description: 'Max results (default 200)' },
          },
        },
      },
      {
        name: 'kg_list_sub_graphs',
        description: 'List all sub-graph domains (Code, Infrastructure, Business, Security, Compliance, People) with node/edge counts.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['tenant_id'],
        },
      },
      {
        name: 'kg_get_sub_graph',
        description: 'Get nodes and edges for a specific sub-graph domain.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            slug: { type: 'string', description: 'Sub-graph slug: code, infrastructure, business, security, compliance, people' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
            limit: { type: 'number', description: 'Max nodes (default 200)' },
          },
          required: ['slug', 'tenant_id'],
        },
      },
      {
        name: 'kg_stats',
        description: 'Get Knowledge Graph statistics — node counts by kind, edge count, confidence distribution, review queue depth.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['tenant_id'],
        },
      },
      {
        name: 'kg_quality',
        description: 'Get the CKG quality dashboard — confidence histogram, conflicts, drift events, review queue.',
        inputSchema: {
          type: 'object' as const,
          properties: {},
        },
      },
      {
        name: 'brain_chat',
        description: 'Ask a question to the Company Brain — RAG-powered chat over the Knowledge Graph. Returns a cited answer.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            message: { type: 'string', description: 'Natural language question to ask the Company Brain' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
            conversation_id: { type: 'string', description: 'Optional conversation ID for follow-up questions' },
          },
          required: ['message', 'tenant_id'],
        },
      },
      {
        name: 'brain_insights',
        description: 'Get Company Brain insights — graph health, anomalies, drift events, review queue, trends.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['tenant_id'],
        },
      },
      {
        name: 'vault_list',
        description: 'List document vaults for a tenant.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['tenant_id'],
        },
      },
      {
        name: 'vault_tree',
        description: 'Get the folder/file tree of a document vault.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            vault_id: { type: 'string', description: 'Vault UUID' },
            prefix: { type: 'string', description: 'Optional path prefix to filter' },
          },
          required: ['vault_id'],
        },
      },
      {
        name: 'vault_get_document',
        description: 'Get the full content of a document from the vault, with metadata and version info.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            vault_id: { type: 'string', description: 'Vault UUID' },
            path: { type: 'string', description: 'Document path, e.g. "architecture/system-overview.md"' },
            ref: { type: 'string', description: 'Optional git ref (commit SHA) to get a specific version' },
          },
          required: ['vault_id', 'path'],
        },
      },
      {
        name: 'vault_search',
        description: 'Search documents in a vault by filename or content.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            vault_id: { type: 'string', description: 'Vault UUID' },
            query: { type: 'string', description: 'Search query' },
            limit: { type: 'number', description: 'Max results (default 20)' },
          },
          required: ['vault_id', 'query'],
        },
      },
      {
        name: 'connector_sync',
        description: 'Trigger a data sync for a connected integration.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            connector_id: { type: 'string', description: 'Connector UUID' },
          },
          required: ['connector_id'],
        },
      },
      {
        name: 'kg_provenance',
        description: 'Get source provenance for a Knowledge Graph node — shows where the knowledge came from, with evidence text and connector sources.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            node_id: { type: 'string', description: 'Node UUID' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['node_id', 'tenant_id'],
        },
      },
      {
        name: 'kg_traverse',
        description: 'Multi-hop graph traversal from a starting node. Returns paths up to N hops with edge evidence.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            start_node_id: { type: 'string', description: 'Starting node UUID' },
            relation: { type: 'string', description: 'Optional relation type to follow' },
            max_hops: { type: 'number', description: 'Maximum hops (default 2, max 3)' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['start_node_id', 'tenant_id'],
        },
      },
      {
        name: 'wiki_get_page',
        description: 'Get a compiled wiki page by path. Returns markdown content with structured frontmatter.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            path: { type: 'string', description: 'Wiki page path, e.g. "systems/billing-api.md"' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['path', 'tenant_id'],
        },
      },
      {
        name: 'wiki_list',
        description: 'List all compiled wiki pages with metadata.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            tenant_id: { type: 'string', description: 'Tenant UUID' },
            subdir: { type: 'string', description: 'Optional subdirectory filter: decisions, projects, people, systems, data, compliance' },
          },
          required: ['tenant_id'],
        },
      },
      {
        name: 'wiki_compile',
        description: 'Compile a wiki page for a Knowledge Graph node on-demand.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            node_id: { type: 'string', description: 'Node UUID to compile a page for' },
            tenant_id: { type: 'string', description: 'Tenant UUID' },
          },
          required: ['node_id', 'tenant_id'],
        },
      },
    ]
  }

  handlers(): Record<string, (args: any) => Promise<any>> {
    return {
      kg_query: async (args) => {
        return this.api(`/api/v1/brain/chat?tenant_id=${args.tenant_id || 'default'}`, {
          method: 'POST',
          body: JSON.stringify({
            message: args.query,
          }),
        })
      },
      kg_get_node: async (args) => {
        return this.api(`/api/v1/ckg/nodes/${encodeURIComponent(args.node_id)}`)
      },
      kg_list_nodes: async (args) => {
        const params = new URLSearchParams()
        if (args.kind) params.set('kind', args.kind)
        if (args.status) params.set('status', args.status || 'promoted')
        return this.api(`/api/v1/ckg/nodes?${params}`)
      },
      kg_list_sub_graphs: async (args) => {
        return this.api(`/api/v1/ckg/sub-graphs?tenant_id=${args.tenant_id}`)
      },
      kg_get_sub_graph: async (args) => {
        const params = new URLSearchParams({ tenant_id: args.tenant_id })
        if (args.limit) params.set('limit', String(args.limit))
        return this.api(`/api/v1/ckg/sub-graphs/${encodeURIComponent(args.slug)}?${params}`)
      },
      kg_stats: async (args) => {
        return this.api(`/api/v1/ckg/stats?tenant_id=${args.tenant_id}`)
      },
      kg_quality: async () => {
        return this.api('/api/v1/ckg/quality')
      },
      brain_chat: async (args) => {
        return this.api(`/api/v1/brain/chat?tenant_id=${args.tenant_id}`, {
          method: 'POST',
          body: JSON.stringify({
            message: args.message,
            conversation_id: args.conversation_id,
          }),
        })
      },
      brain_insights: async (args) => {
        return this.api(`/api/v1/brain/insights?tenant_id=${args.tenant_id}`)
      },
      vault_list: async (args) => {
        return this.api(`/api/v1/vaults?tenant_id=${args.tenant_id}`)
      },
      vault_tree: async (args) => {
        const params = new URLSearchParams()
        if (args.prefix) params.set('prefix', args.prefix)
        return this.api(`/api/v1/vaults/${args.vault_id}/tree?${params}`)
      },
      vault_get_document: async (args) => {
        const params = args.ref ? `?ref=${encodeURIComponent(args.ref)}` : ''
        return this.api(`/api/v1/vaults/${args.vault_id}/documents/${encodeURIComponent(args.path)}${params}`)
      },
      vault_search: async (args) => {
        const params = new URLSearchParams({ q: args.query })
        if (args.limit) params.set('limit', String(args.limit))
        return this.api(`/api/v1/vaults/${args.vault_id}/search?${params}`)
      },
      connector_sync: async (args) => {
        return this.api(`/api/v1/connectors/${args.connector_id}/sync`, {
          method: 'POST',
        })
      },
      kg_provenance: async (args) => {
        return this.api(`/api/v1/ckg/nodes/${encodeURIComponent(args.node_id)}/provenance?tenant_id=${args.tenant_id}`)
      },
      kg_traverse: async (args) => {
        const params = new URLSearchParams({ tenant_id: args.tenant_id })
        if (args.relation) params.set('relation', args.relation)
        if (args.max_hops) params.set('max_hops', String(args.max_hops))
        return this.api(`/api/v1/ckg/nodes/${encodeURIComponent(args.start_node_id)}/traverse?${params}`)
      },
      wiki_get_page: async (args) => {
        return this.api(`/api/v1/wiki/pages/${encodeURIComponent(args.path)}?tenant_id=${args.tenant_id}`)
      },
      wiki_list: async (args) => {
        const params = new URLSearchParams({ tenant_id: args.tenant_id })
        if (args.subdir) params.set('subdir', args.subdir)
        return this.api(`/api/v1/wiki/pages?${params}`)
      },
      wiki_compile: async (args) => {
        return this.api(`/api/v1/wiki/compile/${encodeURIComponent(args.node_id)}?tenant_id=${args.tenant_id}`, {
          method: 'POST',
        })
      },
    }
  }
}
