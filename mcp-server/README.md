# Vouchstone MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes a
Vouchstone control plane's Knowledge Graph, agents, and memory to any MCP
client -- Claude Desktop, Claude Code, OpenCode, or your own MCP-speaking
tooling. Talks to a real control plane over its REST API; every tool and
resource here is a genuine `fetch` call, not a mock.

## Install & build

```bash
cd mcp-server
npm install
npm run build     # tsc -> dist/
npm test          # builds, then spawns the real server over stdio and
                   # exercises it end-to-end (see test/smoke.mjs)
```

## Run

```bash
export VOUCHSTONE_API_URL=https://your-control-plane-host.example.com
export VOUCHSTONE_API_KEY=your-api-key
export VOUCHSTONE_TENANT_ID=your-tenant-id
node dist/index.js
```

The server speaks MCP over stdio -- point an MCP client's config at
`node <path-to>/mcp-server/dist/index.js` (or the packaged
`vouchstone-mcp` bin) with those three env vars set. There is no HTTP
transport; this only ever talks to *your* control plane, over the exact
API surface it already exposes.

## What it exposes

**32 tools** across three categories (`src/tools/`), each a thin,
type-checked wrapper over one real endpoint:

| Category | Tools | Talks to |
|---|---|---|
| Knowledge Graph (`knowledge-graph.ts`) | `kg_query`, `kg_get_node`, `kg_list_nodes`, `kg_list_sub_graphs`, `kg_get_sub_graph`, `kg_stats`, `kg_quality`, `kg_provenance`, `kg_traverse`, `brain_chat`, `brain_insights`, `vault_list`, `vault_tree`, `vault_get_document`, `vault_search`, `connector_sync`, `wiki_get_page`, `wiki_list`, `wiki_compile` (19) | `/api/v1/ckg/*`, `/api/v1/brain/*`, `/api/v1/vaults/*`, `/api/v1/connectors/*`, `/api/v1/wiki/*` |
| Agents (`agents.ts`) | `agent_list`, `agent_get`, `agent_execute`, `agent_ask`, `agent_skills`, `agent_traces`, `agent_pause`, `agent_resume` (8) | `/api/v1/agents/*` |
| Memory (`memory.ts`) | `memory_query_episodic`, `memory_query_semantic`, `memory_query_procedural`, `memory_get_context`, `memory_stats` (5) | see **Known limitations** below |

**4 resources** (`src/resources/knowledge-graph.ts`) -- static, read-only
views for clients that support MCP Resources:

| URI | Backs onto |
|---|---|
| `vouchstone://knowledge-graph/stats` | `GET /api/v1/ckg/stats` |
| `vouchstone://knowledge-graph/sub-graphs` | `GET /api/v1/ckg/sub-graphs` |
| `vouchstone://agents/roster` | `GET /api/v1/agents` |
| `vouchstone://memory/stores` | `GET /api/v1/memory-stores` |

Every one of the routes above was checked against the control plane's
actual mounted routers (`control-plane/backend/app/api/v1/router.py`) --
not assumed from naming convention.

## Known limitations

**`memory.ts`'s five tools call routes that don't exist on the current
backend.** They target `/api/v1/memory-stores/episodic/query`,
`/semantic/query`, `/procedural`, `/context`, and `/stats` -- none of
which are registered; the real memory-read surface today is
`/api/v1/memory-pipeline/{prepare-context,entities/{agent_id},entities/search,skills/{agent_id},snapshot/{agent_id}}`
and `/api/v1/memory-stores/entries` (see
`control-plane/backend/app/api/v1/endpoints/memory_pipeline.py`). Calling
any `memory_*` tool today will surface a real 404 from the control plane
-- an honest failure, not a silent stub -- but the tool is not yet
correctly wired. Fixing it needs a deliberate mapping of each tool's
intended semantics onto the real `memory-pipeline` request/response
shapes, which wasn't done as a drive-by fix here to avoid guessing at
that mapping. Tracked as follow-up work.

## Development

```bash
npm run dev    # tsx src/index.ts, no build step
```

Add a new tool: extend the relevant class in `src/tools/`, add its
`definitions()` entry (name, description, JSON-schema `inputSchema`) and
its `handlers()` entry (an async function making a real `this.api(...)`
call), then re-export from `src/index.ts` if it's a new category. Run
`npm test` before committing -- `test/smoke.mjs` asserts the full tool
list is genuinely non-empty, every tool has a real schema, and no
`ai_*`-prefixed placeholder tools have crept back in (a prior cleanup
removed a batch of fabricated ones -- see `test/smoke.mjs`'s comments).

## License

Apache-2.0 -- Copyright (c) 2026 Vouchstone LLC. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
