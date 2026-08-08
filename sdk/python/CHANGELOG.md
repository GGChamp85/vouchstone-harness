# Changelog

All notable changes to the Vouchstone Python SDK are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.0] — 2026-08-08

### Added — OpenCode integration
- **Agent interop** (`vouchstone_sdk.opencode`): export any Vouchstone agent
  to OpenCode's format (`.opencode/agents/<name>.md` — description, mode,
  model, temperature, `permission:` rules **derived from the agent's enforced
  Scope + posture**; out-of-boundary tools become `deny`, STRICT escalates to
  `ask`) and import edited files back losslessly (scope/posture round-trip via
  `x-vouchstone` metadata).
- **Governed imports** — `governed_import()` / `vouchstone opencode
  import-agent --governed`: an OpenCode edit of an agent definition passes
  through Forge's `CompatibilityGate` (agent-definition edits carry
  `log_to_audit` + `require_dual_signoff` obligations) and lands on a
  hash-chained trace — including denials and parse failures.
- **Skills interop** — skill runbooks export to OpenCode skill files;
  `copy_skill()` clones a skill between agents through the real
  `ProceduralMemory` registry with a fresh track record.
- **Workspace scaffold** — `vouchstone opencode init [--from-kg kg.json]`
  writes agents (optionally auto-derived from a KG artifact, scoped), skills,
  Vouchstone slash-commands, and `opencode.json` pre-wired with Vouchstone's
  MCP server so OpenCode sessions query the live KG/memory/vault.
- **Optimization** — `vouchstone opencode optimize-agent` drives the control
  plane's real Optimization Studio (`POST /optimization/runs`).
- **OpenCode adapter verified** — file **deletions** now appear in engine
  diffs (previously invisible), and a real-binary integration test runs in CI
  (`sdk-ci.yml` installs `opencode-ai`), closing the long-documented
  "authored without the binary available" gap; auto-skipped locally.

### Added — Harness pillar
- **Unified LLM core** (`vouchstone_sdk.llm`): one provider-agnostic chat
  interface with normalized tool calling. Built-in providers: `openai`,
  `anthropic`, and **`openrouter`** — the any-LLM gateway (set
  `OPENROUTER_API_KEY`, use `openrouter/<vendor>/<model>` model strings) so
  enterprises run the harness on any model on the market. Additional gateways
  plug in via the `vouchstone.llm_providers` entry-point group.
- **`HarnessAgent`** (`vouchstone_sdk.harness`): the governed tool-use loop —
  every tool call is evaluated against a deny-by-default `PolicyGraph`
  *before* it executes, every event (turn, tool call, result, denial,
  approval) is appended to a hash-chained `WorkflowTrace`, and denials are
  returned to the model as tool errors instead of hallucinated results.
- **`ToolRegistry`** — real JSON-schema `parameters` derived from function
  signatures/type hints, with actual dispatch (sync and async tools).
  `Agent.register_tool` historically recorded empty schemas and had no
  dispatch path.
- **Security postures** (`HarnessPosture.AUTO` / `STRICT`): under STRICT, any
  policy obligation requires a synchronous human approval callback; approvals
  and rejections are trace entries attributed to `actor="human"`.
- **`Scope`** — an *enforced* KG boundary: bounds memory retrieval
  (domains/entity kinds/tags), compiles `allowed_tools` into the policy
  graph's only permits, and namespaces memory keys for per-team isolation.
- **`CommandPolicy` + `make_shell_tool`** — a predeclared allow/deny-pattern
  command policy (deny-by-default, deny overrides allow) backing a governed
  shell tool.

### Added — KG pillar
- **Signed knowledge-graph artifacts** (`vouchstone_sdk.kg`): point the SDK at
  any directory and get a committable, tamper-evident JSON graph —
  deterministic stdlib-`ast` extraction (modules/classes/functions,
  contains/imports edges), a manifest hash-chained with the same scheme as the
  control plane's signed ledger, incremental rebuilds (unchanged files are
  never re-parsed; unchanged trees produce byte-identical signatures),
  entity-level diffs, and offline verification.
- **`vouchstone` CLI** (`vouchstone kg build|verify|diff|agents`) — works on a
  bare core install, air-gapped.
- **Agent discovery from the graph** — `vouchstone kg agents` /
  `propose_agents_from_artifact()` derive scoped specialist-agent candidates
  (domains + entity kinds + persona draft) from the artifact's own domain
  distribution; `AgentCandidate.to_agent_config_kwargs()` yields a ready
  `AgentConfig`. Offline counterpart of the control plane's
  `POST /workforce/agents/suggest-from-kg`.
- **Artifact-grounded memory** — `seed_pipeline_from_artifact()` upserts a
  graph's entities into an agent's semantic memory in every backend mode.
- **Unified KG schema** — ingestion output now converges on the canonical
  `types.Entity`/`EntityGraph` shape (`to_canonical_entity`,
  `BaseIngester.build_graph`), and `build_source_artifact()` produces the same
  signed artifact from a live source (Slack/Jira/...) as a codebase build.
- **Extraction strategies are real** — the previously empty
  `EXTRACTION_STRATEGIES` registry now drives `BaseIngester.extract_entities`
  with two built-ins: `"llm"` (default) and `"deterministic"` (zero-LLM,
  air-gap-safe); third parties plug in via entry points.

### Changed
- **Dependency hygiene (breaking for implicit users):** `openai` and `anthropic`
  are no longer core dependencies — install them via the `llm-openai` /
  `llm-anthropic` extras (both included in `all`). Every LLM-touching code path
  raises a clear `ImportError` naming the exact extra to install. `numpy` was
  removed outright (declared but never imported).
- `setup.py` removed; `pyproject.toml` is the single packaging source of truth.
- The package now ships a `py.typed` marker — downstream `mypy` sees SDK types.

### Added
- `MemoryBackendUnavailableError` is exported from the package root (it is the
  error callers must catch around `Agent.initialize()` / `MemoryPipeline.initialize()`).
- `ruff` and `mypy` configurations; the whole package is lint- and type-clean.

### Fixed
- `OpenCodeEngineAdapter` no longer performs blocking filesystem work
  (materialize/diff of the working tree) on the event loop.
- Anthropic response parsing in `ClaudeEngineAdapter` is safe for non-text
  content blocks.
- `replay_and_verify()` explicitly reports a missing `template_id` in the trace
  payload instead of failing on a `None` lookup.

## [1.5.0] — 2026-08

Baseline release this changelog starts from. Highlights of what ships:

- `Agent`/`AgentConfig` with the 5-layer memory pipeline (`MemoryPipeline`),
  concurrency-safe checkpointing via `contextvars`.
- Graph primitives: `EntityGraph`, `PolicyGraph` (deny-by-default),
  `WorkflowTrace` (sha256 hash-chained, verifiable).
- Vouchstone Forge: engine adapters (`OpenCodeEngineAdapter` — default,
  `ClaudeEngineAdapter`, `EchoEngineAdapter`), `CompatibilityGate`,
  `SandboxRunner`, signed trace per change request.
- Deterministic Transformation Engine with `replay_and_verify()`.
- `VaultClient`, `DomainClient`, `VouchstoneClient` control-plane clients.
- Ingestion package: Slack, Jira, Confluence, GitHub, Meetings ingesters with
  LLM entity/relationship extraction.
- Local eval harness, OpenTelemetry integration, entry-points plugin registry.
