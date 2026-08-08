# Vouchstone Open Harness

The open-source data plane for the [Vouchstone](https://vouchstone.ai) Enterprise AI Agent Platform — the part that runs inside your own environment (VPC or fully air-gapped), not in Vouchstone's cloud.

Licensed **Apache-2.0**. The hosted Control Plane (multi-tenant SaaS: extraction pipeline, governance, billing) is separate, proprietary software — see [vouchstone.ai](https://vouchstone.ai). This repo is the part you can read, run, fork, and modify.

## What's here

- **`sdk/python/`** — the Vouchstone SDK (Python-only by design): `Agent`, the 5-layer memory pipeline (Working/Episodic/Semantic/Procedural/Meta), `EntityGraph`/`PolicyGraph`/`WorkflowTrace`, `Forge` (framework-agnostic agent-customization orchestrator with a compatibility gate and sandboxed execution), the Deterministic Transformation Engine, OpenTelemetry instrumentation, a local eval harness, and a real plugin model (`importlib.metadata` entry_points).
- **`runtime/`** — the agent runtime, including **offline harness mode**: pull a signed bundle once, then operate with zero network calls against a local KG snapshot. Also: durable execution tracking across restarts, dependency-scanned bundles (`harness scan`), KG schema versioning/migration, and **sovereign deployment mode** — a hard network-egress guard for buyers who need inference itself to never leave their own infrastructure.
- **`mcp-server/`** — a real [Model Context Protocol](https://modelcontextprotocol.io) server exposing Knowledge Graph, agent, memory, and governance operations to any MCP client (Claude Desktop, Cursor, etc.) via `@modelcontextprotocol/sdk` and stdio transport.

## Install

```bash
pip install vouchstone-sdk
```

See each subdirectory's own README for details:
- [`sdk/python/README.md`](sdk/python/README.md)
- [`runtime/README.md`](runtime/README.md)

## License

Apache License 2.0 — see [LICENSE](LICENSE). "Vouchstone" and "Forge" are trademarks of Vouchstone LLC; trademark usage is governed separately by [TRADEMARKS.md](TRADEMARKS.md) (Apache-2.0 doesn't grant trademark rights).

## Links

- [vouchstone.ai](https://vouchstone.ai)
- Full platform source (control plane + this harness): [github.com/GGChamp85/Vouchstone](https://github.com/GGChamp85/Vouchstone)
