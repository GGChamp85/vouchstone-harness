# Contributing to the Vouchstone Python SDK

Thanks for your interest in contributing. This SDK is Apache-2.0 licensed and
contributions are welcome — bug reports, fixes, docs, and well-scoped features.

## Ground rules

1. **No stubs, no mocks-as-features.** This project has a hard standard: code
   either does the real thing or raises a clear error explaining what is
   missing. Silent fallbacks that fake success are rejected in review.
2. **Every bug fix ships with a regression test** that fails before the fix
   and passes after.
3. **Public API changes need a CHANGELOG entry** (Keep-a-Changelog format,
   under `[Unreleased]`).
4. **Optional dependencies stay optional.** Anything touching `openai`,
   `anthropic`, `redis`, `chromadb`, `neo4j`, `asyncpg`, or `opentelemetry`
   must import lazily and raise an `ImportError` naming the extra to install.

## Development setup

```bash
cd data-plane/sdk/python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Before you open a PR

All three must pass locally — CI enforces them:

```bash
ruff check vouchstone_sdk/     # lint
mypy                           # type check (config in pyproject.toml)
pytest tests/ -v               # full test suite
```

## Testing conventions

- Async tests run under `asyncio_mode = "auto"` — no decorator needed.
- HTTP clients are tested against `httpx.MockTransport` with real
  request-building asserted (see `tests/test_domain.py` for the pattern).
- Engine adapters that shell out are tested against a fake binary on `PATH`
  (see `tests/test_opencode_engine.py`); integration tests against real
  binaries are auto-skipped when the binary is absent.

## Plugins

Third-party engine adapters, extraction strategies, and eval graders register
via `importlib.metadata` entry points — groups `vouchstone.engine_adapters`,
`vouchstone.extraction_strategies`, `vouchstone.eval_graders`. See
`tests/fixtures/example_plugin_pkg/` for a complete installable example.

## Reporting security issues

Do **not** open a public issue — see [SECURITY.md](SECURITY.md).
