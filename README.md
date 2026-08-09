# Vouchstone Open Harness

**Graph-anchored harness. Infinite supervised agentic scale — inside your own environment.**

The open-source data plane for the [Vouchstone](https://vouchstone.ai) Enterprise AI
Agent Platform: agents that are **anchored to a verifiable knowledge graph** and run
inside a **governed harness** where no tool fires unchecked and every step is
hash-chained — running in *your* VPC, or fully air-gapped, not in Vouchstone's cloud.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![vouchstone-sdk on PyPI](https://img.shields.io/pypi/v/vouchstone-sdk.svg)](https://pypi.org/project/vouchstone-sdk/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](sdk/python/pyproject.toml)

Vouchstone LLC | [Website](https://vouchstone.ai) | [Docs](https://vouchstone.ai/docs) | [Full platform repo](https://github.com/GGChamp85/Vouchstone)

Licensed **Apache-2.0** end to end. The hosted Control Plane (multi-tenant SaaS:
5-pass extraction pipeline, Document Vault moderation, Action Gateway governance,
billing) is separate, proprietary software — see [vouchstone.ai](https://vouchstone.ai).
Everything in *this* repo you can read, run, fork, and modify.

---

## What's here

Three components, each independently usable, each real and working — not a demo
shell around a proprietary core.

### [`sdk/python/`](sdk/python/) — the Vouchstone SDK

The Python-only SDK covering both pillars end to end:

- **Knowledge Graph** — `vouchstone kg build` turns a directory (or a live Slack/Jira/
  Confluence/GitHub/meetings source) into a signed, hash-chained, committable JSON
  artifact. `vouchstone kg agents` derives scoped specialist-agent candidates directly
  from the graph's own domain distribution — no LLM required for either.
- **Dynamic Agent Harness** — `HarnessAgent`: a governed tool-use loop where every
  call is evaluated against a deny-by-default `PolicyGraph` *before* it executes and
  every turn appends to a hash-chained `WorkflowTrace`. `Scope` is the enforced KG
  boundary an agent cannot act outside of. Runs on OpenAI, Anthropic, or any model on
  the market via the built-in OpenRouter provider.
- **OpenCode bridge** — export agents to [OpenCode](https://opencode.ai)'s
  `.opencode/agents/*.md` format (permissions derived from each agent's `Scope`), edit
  them with full AI assistance, and import edits back **through the same governance
  gate** — with skills, MCP access to your live KG/memory/vault, and Vouchstone
  slash-commands scaffolded by `vouchstone opencode init`.
- Also: the 5-layer memory pipeline (Working/Episodic/Semantic/Procedural/Meta),
  `EntityGraph`/`PolicyGraph`/`WorkflowTrace` as reusable primitives, `Forge`
  (framework-agnostic agent-customization orchestrator: engine → compatibility gate →
  sandbox → signed trace), the Deterministic Transformation Engine (byte-identical
  replay of past changes), OpenTelemetry instrumentation, a local eval harness, and a
  real plugin model (`importlib.metadata` entry_points for engines, extraction
  strategies, and eval graders).

→ [`sdk/python/README.md`](sdk/python/README.md) for the full quickstart and API reference.

### [`runtime/`](runtime/) — the agent runtime

Executes agents built with the SDK, two ways:

- **Connected** — talks to a live control plane for agent specs, heartbeats, and
  ledger replay.
- **Offline harness mode** — pull a signed bundle once (`harness pull`), then operate
  with **zero network calls** against a local, schema-versioned KG snapshot
  (`LocalKGStore`) that survives process restarts. `harness verify`/`harness scan` are
  built as real CI/CD pipeline gates — distinct exit codes, not just human-readable
  output.
- **Sovereign deployment mode** — for buyers where no external AI-vendor dependency is
  acceptable at all, inference included: a hard startup guard (static config check +
  a real concurrent TCP-reachability probe against known external LLM hosts) that
  refuses to start rather than logging a warning.
- Durable execution tracking across restarts (`ExecutionStore`), dependency-scanned
  bundles wrapping the real `pip-audit` CLI.

→ [`runtime/README.md`](runtime/README.md) for bundle format, sovereign mode, and CI/CD integration.

### [`mcp-server/`](mcp-server/) — the MCP server

A real [Model Context Protocol](https://modelcontextprotocol.io) server exposing your
control plane's Knowledge Graph, agents, and memory to any MCP client — Claude
Desktop, Claude Code, OpenCode, or your own tooling. **32 tools + 4 resources**, each a
thin, type-checked wrapper over a real control-plane endpoint (checked against the
actual FastAPI routers, not assumed from naming convention) — stdio transport only,
so this only ever talks to *your own* control plane.

→ [`mcp-server/README.md`](mcp-server/README.md) for the full tool/resource reference.

---

## Architecture

```
                    YOUR ENVIRONMENT (this repo)
        +----------------------------------------------+
        |                                                |
        |   sdk/python/        runtime/                  |
        |   Agent, Harness,    Agent execution,           |
        |   KG artifacts,      offline harness bundles,   |
        |   Forge, memory      sovereign mode              |
        |         |                   |                   |
        |         +-------------------+                   |
        |                   |                              |
        |            mcp-server/                           |
        |       (any MCP client talks to                   |
        |        your control plane through here)          |
        +----------------------------------------------+
                            |
                     optional, HTTPS
                            v
        +----------------------------------------------+
        |     VOUCHSTONE CONTROL PLANE (proprietary)     |
        |  Dashboard · 5-pass extraction · Document Vault |
        |  Action Gateway · Signed Ledger · Evals/Optimize|
        +----------------------------------------------+
```

The connection to a control plane is **optional at every layer**: the KG pillar, the
harness, Forge, evals, and the OpenCode bridge all run standalone or fully
air-gapped. Connect a control plane (self-hosted or Vouchstone cloud) when you want
hosted memory, the Vault, team-wide governance, or a live MCP backend.

---

## Quick start

```bash
pip install vouchstone-sdk

# Pillar 1 — point at a codebase, get a signed, verifiable knowledge graph
vouchstone kg build ./your-repo -o kg.json
vouchstone kg verify kg.json
vouchstone kg agents kg.json          # the graph proposes its own scoped agents
```

```python
# Pillar 2 — run a governed agent: every tool call checked against a
# deny-by-default policy graph before it executes, every turn hash-chained.
import asyncio
from vouchstone_sdk import AgentConfig, HarnessAgent, HarnessPosture, Scope, ToolRegistry, Message

def lookup_invoice(invoice_id: str) -> dict:
    """Look up an invoice by id."""
    return {"invoice_id": invoice_id, "amount": 1200}

tools = ToolRegistry()
tools.register(lookup_invoice)

agent = HarnessAgent(
    AgentConfig(name="ap-specialist", model="openrouter/anthropic/claude-sonnet-4-6"),
    tools=tools,
    scope=Scope(domains=["finance"], allowed_tools=["lookup_invoice"]),
    posture=HarnessPosture.STRICT,      # obligations require human approval
)

async def main():
    await agent.initialize(agent_id="ap-specialist", local_only=True)
    agent.start_session()
    response = await agent.process(Message(content="How much is INV-9?"))
    print(response.content)
    print("verifiable:", agent.trace.verify_chain(), agent.trace.tip_hash)

asyncio.run(main())
```

Any tool outside the scope is denied *before* execution. See
[`sdk/python/README.md`](sdk/python/README.md) for the full Quick Start (KG discovery,
OpenCode editing, the control plane client, and every other component), and the
`runtime/`/`mcp-server/` READMEs linked above for those two — both are independently
installable and don't require the SDK's CLI.

---

## Why enterprises use this

| Enterprise requirement | What this repo does about it |
|---|---|
| *"Prove what the agent knew."* | Signed KG artifacts: the exact grounding is committable, diffable, and offline-verifiable. |
| *"No agent acts outside its mandate."* | `Scope` compiles into the policy graph's only permits — deny-by-default, per tool call, before execution. |
| *"Auditable six months later."* | Hash-chained `WorkflowTrace` on every run and every governed change, using the same canonical-JSON + SHA-256 scheme as the control plane's signed ledger. |
| *"Data never leaves our network."* | The offline harness bundle + `LocalKGStore` operate with zero network calls; sovereign mode adds a hard, independently-verifiable egress guard for inference itself. |
| *"No model lock-in."* | One LLM core, three providers built in (OpenAI / Anthropic / OpenRouter → any model), pluggable via entry points. |
| *"No vendor lock-in on tooling."* | Agents export to OpenCode's open format; skills are markdown; graphs are JSON; the MCP server works with any MCP client. Everything works air-gapped. |
| *"Our security team reviews everything."* | Lean core deps, `pip-audit`/`npm audit` clean, `py.typed`, real dependency-vulnerability scanning built into the runtime CLI (`harness scan`) as a CI/CD gate. |

## Standalone OSS vs. Enterprise Platform

Everything in this repository is Apache-2.0 and works without a Vouchstone account.
The hosted/enterprise control plane adds the team- and compliance-grade layer on top
of the same primitives:

| Capability | OSS (this repo) | + Vouchstone Enterprise |
|---|---|---|
| Knowledge graph | Signed local artifacts, deterministic + optional LLM pass | Hosted 5-pass LLM extraction pipeline, 69-connector catalog, Document Vault moderation (Raw→Workspace→Canonical), auto-compiled Wiki, Company Brain RAG with cited answers |
| Agent harness | Governed tool loop, scopes, postures, local traces | Action Gateway with Constitution/Authority-Matrix policy, autonomy levels (L0–L4), approval queues, tenant-wide signed ledger with replay |
| Deployment | Connected or fully offline/sovereign, you operate it | Monitoring, usage billing, SLAs, enterprise support |
| Memory | 5 layers with your own Redis/Chroma/Neo4j (or in-process) | Hosted multi-tenant memory with Meta-Memory governance (decay, dedup, compression) run for you |
| Agent discovery | From local artifacts (`kg agents`) | From the live Customer Knowledge Graph, with the Strategy Council verifying answers |
| Evals & optimization | Local eval harness | Evals dashboards, cost-per-run billing, DSPy Optimization Studio |

The upgrade path is incremental: point the SDK's `VouchstoneClient` at a control plane
and the same code you wrote against local backends starts using hosted ones. Talk to
[renu@vouchstone.ai](mailto:renu@vouchstone.ai) or see
[vouchstone.ai/pricing](https://vouchstone.ai/pricing).

---

## Development

Each component is tooled and tested independently — there's no unified root-level
test runner yet:

```bash
cd sdk/python  && pip install -e ".[all,dev]" && pytest tests/ -v
cd runtime     && pip install -r requirements-dev.txt && pytest tests/ -v
cd mcp-server  && npm install && npm test
```

There is no CI workflow wired up for this repo yet (the private monorepo's CI covers
these same test suites as part of a larger pipeline) — run the commands above before
opening a PR.

## Contributing, changelog, and security

- [CONTRIBUTING.md](CONTRIBUTING.md) — ground rules and dev setup for all three components.
- [CHANGELOG.md](CHANGELOG.md) — repo-level history (`sdk/python` also keeps its own [package changelog](sdk/python/CHANGELOG.md)).
- [SECURITY.md](SECURITY.md) — how to report a vulnerability, and scope notes for deployers.
- [CODEOWNERS](CODEOWNERS) — who reviews what.

## License

Apache License 2.0 — see [LICENSE](LICENSE). "Vouchstone" and "Forge" are trademarks
of Vouchstone LLC; trademark usage is governed separately by
[TRADEMARKS.md](TRADEMARKS.md) (Apache-2.0 doesn't grant trademark rights).

## Links

- [vouchstone.ai](https://vouchstone.ai)
- [vouchstone-sdk on PyPI](https://pypi.org/project/vouchstone-sdk/)
- Full platform source (control plane + this harness): [github.com/GGChamp85/Vouchstone](https://github.com/GGChamp85/Vouchstone)
