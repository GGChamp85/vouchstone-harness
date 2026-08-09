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
| Memory (`memory.ts`) | `memory_query_episodic`, `memory_query_semantic`, `memory_query_procedural`, `memory_get_context`, `memory_stats` (5) | `/api/v1/memory-stores/entries/search`, `/api/v1/memory-pipeline/*` |

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
not assumed from naming convention. Every route that requires `tenant_id`
(nearly all of them -- this is a multi-tenant API where it's an explicit
query param, not derived from the auth header alone) gets it from
`VOUCHSTONE_TENANT_ID`, threaded through each tool/resource class's
constructor.

### Memory tools: real endpoint mapping

`memory.ts`'s five tools don't map 1:1 onto a single "memory API" --
there isn't one. Each is wired to whichever real, read-only endpoint
actually backs its stated semantics:

| Tool | Real endpoint | Notes |
|---|---|---|
| `memory_query_episodic` | `POST /api/v1/memory-stores/entries/search` (`memory_type=episodic`) | Full-text (ILIKE) search, not vector. `agent_id` optional -- omit to search all agents in the tenant. |
| `memory_query_semantic` | `POST /api/v1/memory-pipeline/entities/search` | Real vector search when embeddings are configured (falls back to text search) -- `agent_id` required, the route is agent-scoped. `entity_type` isn't a route-level filter, so it's applied client-side after retrieval. |
| `memory_query_procedural` | `GET /api/v1/memory-pipeline/skills/{agent_id}` | `agent_id` required. `skill_name`/`min_confidence` aren't route-level filters either -- applied client-side against the returned `skill_name`/`success_rate` fields. |
| `memory_get_context` | `POST /api/v1/memory-pipeline/prepare-context` | The real 4-layer retrieval a live agent turn uses. It has a genuine side effect (appends a working-memory entry for the `session_id` you pass), so this tool always generates a fresh `mcp-scratch-<uuid>` session_id per call -- it never reads or writes a real agent session. |
| `memory_stats` | `GET /api/v1/memory-pipeline/snapshot/{agent_id}` | Episodic/semantic/procedural counts, sample entities/skills, plus the tenant's meta-memory health report. `agent_id` required -- there's no tenant-wide aggregate endpoint. |

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
