type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class KnowledgeGraphResources {
  constructor(private api: ApiCall, private tenantId: string) {}

  definitions() {
    return [
      {
        uri: 'vouchstone://knowledge-graph/stats',
        name: 'Knowledge Graph Statistics',
        description: 'Node/edge counts, counts by kind, average confidence, pending-review depth, recent drift events',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://knowledge-graph/sub-graphs',
        name: 'Sub-Graph Index',
        description: 'Available domain sub-graphs with rollup node/edge counts and health scores',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://agents/roster',
        name: 'Agent Roster',
        description: 'All agents for this tenant with name, status, and config summary',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://memory/stores',
        name: 'Memory Store List',
        description: 'Configured memory stores for this tenant (type, provider, status) -- per-store detail is available via the memory_stats tool',
        mimeType: 'application/json',
      },
    ]
  }

  async read(uri: string): Promise<any> {
    const routes: Record<string, string> = {
      'vouchstone://knowledge-graph/stats': `/api/v1/ckg/stats?tenant_id=${this.tenantId}`,
      'vouchstone://knowledge-graph/sub-graphs': `/api/v1/ckg/sub-graphs?tenant_id=${this.tenantId}`,
      'vouchstone://agents/roster': `/api/v1/agents?tenant_id=${this.tenantId}`,
      'vouchstone://memory/stores': `/api/v1/memory-stores?tenant_id=${this.tenantId}`,
    }
    const path = routes[uri]
    if (!path) throw new Error(`Unknown resource: ${uri}`)
    return this.api(path)
  }
}
