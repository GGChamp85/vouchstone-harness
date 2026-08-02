type ApiCall = (path: string, options?: RequestInit) => Promise<any>

export class KnowledgeGraphResources {
  constructor(private api: ApiCall) {}

  definitions() {
    return [
      {
        uri: 'vouchstone://knowledge-graph/stats',
        name: 'Knowledge Graph Statistics',
        description: 'Entity counts by type, relationship counts, sub-graph summaries, last ingestion time',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://knowledge-graph/entity-types',
        name: 'Entity Type Catalog',
        description: 'All entity types in the KG with attribute schemas and counts',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://knowledge-graph/relationship-types',
        name: 'Relationship Types',
        description: 'All relationship types with source/target entity type constraints and counts',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://knowledge-graph/sub-graphs',
        name: 'Sub-Graph Index',
        description: 'Available sub-graphs with entity counts and key nodes',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://agents/roster',
        name: 'Agent Roster',
        description: 'All agents with name, role, tier, status, and skill summary',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://memory/layer-stats',
        name: 'Memory Layer Statistics',
        description: 'Stats for all 5 memory layers: working, episodic, semantic, procedural, meta',
        mimeType: 'application/json',
      },
      {
        uri: 'vouchstone://governance/policy-summary',
        name: 'Governance Policy Summary',
        description: 'Active ABAC policies, RACI matrix, budget status, vendor risk posture',
        mimeType: 'application/json',
      },
    ]
  }

  async read(uri: string): Promise<any> {
    const routes: Record<string, string> = {
      'vouchstone://knowledge-graph/stats': '/api/v1/knowledge-graph/stats',
      'vouchstone://knowledge-graph/entity-types': '/api/v1/knowledge-graph/entity-types',
      'vouchstone://knowledge-graph/relationship-types': '/api/v1/knowledge-graph/relationship-types',
      'vouchstone://knowledge-graph/sub-graphs': '/api/v1/knowledge-graph/sub-graphs',
      'vouchstone://agents/roster': '/api/v1/agents',
      'vouchstone://memory/layer-stats': '/api/v1/memory-stores/stats',
      'vouchstone://governance/policy-summary': '/api/v1/governance/summary',
    }
    const path = routes[uri]
    if (!path) throw new Error(`Unknown resource: ${uri}`)
    return this.api(path)
  }
}
