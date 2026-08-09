# Changelog

Repo-level changes across all three components. Follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions; dated
rather than versioned, since `runtime/` and `mcp-server/` aren't
independently released yet. For `sdk/python`'s own package-version history,
see [`sdk/python/CHANGELOG.md`](sdk/python/CHANGELOG.md).

## 2026-08-08

### Fixed
- **`mcp-server`**: five `memory_*` tools called routes that never existed on
  the control plane (`/api/v1/memory-stores/episodic/query`, `/semantic/query`,
  `/procedural`, `/context`, `/stats`). Remapped each onto real, verified
  endpoints across `/api/v1/memory-stores/entries/search` and
  `/api/v1/memory-pipeline/*`, including real vector search for semantic
  queries and an isolated scratch session for context retrieval so it never
  touches a live agent session.
- **`mcp-server`**: removed the `governance` tool category (`governance.ts`)
  and the resources it exposed — both pointed at control-plane routes removed
  in an earlier backend restructure. Also fixed `resources/knowledge-graph.ts`,
  which pointed at a `/api/v1/knowledge-graph/*` prefix that was never real
  (the actual routes are `/api/v1/ckg/*`) and never passed the `tenant_id`
  every one of those endpoints requires.
- **`mcp-server`**: added the missing `test` npm script; `npm audit fix` for a
  moderate hono ReDoS/data-disclosure CVE.
- **`runtime`**: synced drift fixes from the private monorepo — multi-replica
  Redis wiring for Working Memory, `harness status` surfacing Forge-engine
  availability, and a concurrency-safety test fix.
- **`runtime`**: dropped 9 declared-but-never-imported dependencies
  (`torch`, `transformers`, `chromadb`, `qdrant-client`, `neo4j`, `celery`,
  `anthropic`, `python-multipart`, `python-jose`); switched from an editable
  SDK install to a real `vouchstone-sdk[redis,vector,graph]` PyPI pin.
- **`runtime`**: found and fixed a real bug in `Dockerfile.optimized`'s
  multi-stage build — `pip install --user` resolved its target directory from
  `$HOME` at build time (root) but the final image runs as a non-root user
  with a different `$HOME`, so `import vouchstone_sdk` failed at container
  startup. Switched the builder stage to a venv.
- **`sdk/python`**: `mypy`'s CI gate failed on a transitive dependency
  (`numpy`, via `chromadb`) shipping stub syntax newer than the configured
  target Python version; fixed by targeting the same version CI actually
  runs under.
- **`sdk/python`**: README no longer led with a generic example disconnected
  from the "graph-anchored harness" pitch; reordered the Quick Start to match,
  and fixed several drifted claims (stale default model string, a demoted
  example that referenced a non-existent `self.llm` attribute, stale plugin
  registry claims).

### Removed
- **`sdk/typescript/`** — abandoned since its initial commit, never
  referenced by CI or documentation beyond one README bullet. The SDK is
  Python-only by design.

## 2026-08-02 — Initial publish

First publish of the open harness: `sdk/python/` (the Vouchstone SDK),
`runtime/` (the agent runtime, including offline harness mode and sovereign
deployment mode), and `mcp-server/` (the Model Context Protocol server).
