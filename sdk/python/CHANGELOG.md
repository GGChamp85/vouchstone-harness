# Changelog

All notable changes to the Vouchstone Python SDK are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
