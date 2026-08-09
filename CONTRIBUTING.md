# Contributing to Vouchstone Open Harness

Thanks for your interest in contributing. This repo is Apache-2.0 licensed end to
end, and contributions are welcome — bug reports, fixes, docs, and well-scoped
features across any of the three components (`sdk/python/`, `runtime/`,
`mcp-server/`).

## Ground rules

1. **No stubs, no mocks-as-features.** Code either does the real thing (a genuine
   API call, a genuine computation) or raises a clear error explaining what's
   missing. Silent fallbacks that fake success are rejected in review. This has
   been a hard standard across every fix in this repo's history — see the
   [CHANGELOG](CHANGELOG.md) for examples of exactly this class of bug being
   found and fixed.
2. **Every bug fix ships with a regression test** that fails before the fix and
   passes after (or, where a live external system can't be part of CI, a clear
   note in the PR of how you verified the fix against the real thing).
3. **Route/endpoint changes get verified against the real backend**, not assumed
   from naming convention — several bugs fixed in this repo were exactly this:
   a client (SDK, runtime, or MCP tool) calling a path that looked plausible but
   didn't match what the control plane actually mounts.
4. **Optional dependencies stay optional** (SDK specifically) — anything
   touching `openai`, `anthropic`, `redis`, `chromadb`, `neo4j`, `asyncpg`, or
   `opentelemetry` must import lazily and raise an `ImportError` naming the
   extra to install.
5. **Docs and code drift together.** If you change a route, a CLI flag, or a
   public API, update the README/CHANGELOG in the same PR — this repo has had
   real bugs caused purely by docs/tests drifting out of sync with a change
   made elsewhere.

## Component-specific guides

- **`sdk/python/`** has its own [CONTRIBUTING.md](sdk/python/CONTRIBUTING.md)
  (dev setup, plugin conventions, testing patterns) and
  [SECURITY.md](sdk/python/SECURITY.md).
- **`runtime/`** and **`mcp-server/`** don't have separate contributor guides
  yet — the ground rules above apply, plus each component's own README for
  setup.

## Development setup (all three components)

```bash
cd sdk/python  && pip install -e ".[all,dev]" && pytest tests/ -v
cd runtime     && pip install -r requirements-dev.txt && pytest tests/ -v
cd mcp-server  && npm install && npm test
```

There's no unified root-level test runner yet — see each component's README for
the exact gate (lint/type-check/test) to run before opening a PR.

## Reporting security issues

Do **not** open a public issue — see [SECURITY.md](SECURITY.md).

## Pull requests

- Keep PRs scoped to one component where possible; cross-component PRs (e.g. a
  route the SDK, runtime, and MCP server all need to agree on) are fine, just
  say so in the description.
- Reference the file:line of anything you're fixing when the PR is a bug fix —
  it makes review much faster and is the standard this repo's own history holds
  itself to.
