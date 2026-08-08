# Changelog

All notable changes to the Vouchstone Python SDK are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
