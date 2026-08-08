# Vouchstone SDK

**Graph-anchored harness. Infinite supervised agentic scale.**

The open-source Python SDK for the Vouchstone Enterprise AI Agent Platform —
build agents that are **anchored to a verifiable knowledge graph** and run
inside a **governed harness** where no tool fires unchecked and every step
is hash-chained.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-177%20passing-brightgreen.svg)](tests/)

Vouchstone LLC | [Website](https://vouchstone.ai) | [Docs](https://vouchstone.ai/docs) | [Platform repo](https://github.com/GGChamp85/Vouchstone)

---

## The two pillars

**1. Knowledge Graph — point at anything, get a signed, verifiable graph.**

```bash
pip install vouchstone-sdk
vouchstone kg build ./your-repo -o kg.json     # deterministic, offline, no LLM needed
vouchstone kg verify kg.json                   # tamper-evident: ledger-style hash chain
vouchstone kg agents kg.json                   # the graph proposes its own scoped agents
```

Every artifact is a committable JSON file whose manifest is hash-chained with
the same scheme as Vouchstone's signed ledger: anyone can verify — offline —
that neither the graph nor its recorded sources were altered. Rebuilds are
incremental (unchanged files are never re-parsed); unchanged trees produce
byte-identical signatures. Five real source ingesters (Slack, Jira,
Confluence, GitHub, Meetings) feed the **same** signed artifact format.

**2. Dynamic Agent Harness — the governed tool-use loop.**

Every tool call is evaluated against a **deny-by-default policy graph before
it executes**. Every event — turn, tool call, result, denial, human approval
— lands on a hash-chained trace an auditor can replay. An agent's Knowledge-
Graph **scope is enforced, not advisory**: out-of-boundary tools are
structurally impossible, and denials go back to the model as tool errors so
it adapts instead of hallucinating results. Run it on **any LLM** — OpenAI,
Anthropic, or anything on the market via the built-in OpenRouter provider
(`openrouter/<vendor>/<model>` + `OPENROUTER_API_KEY`).

Plus the **OpenCode bridge**: export your agents to
[OpenCode](https://opencode.ai) (`.opencode/agents/*.md`, permissions derived
from each agent's enforced scope), edit them with full AI assistance, and
import them back **through the same governance gate** — with skills, MCP
access to your live KG/memory/vault, and Vouchstone slash-commands scaffolded
by `vouchstone opencode init`.

---

## Why enterprises use this

| Enterprise requirement | What the SDK does about it |
|---|---|
| *"Prove what the agent knew."* | Signed KG artifacts: the exact grounding is committable, diffable, and offline-verifiable. |
| *"No agent acts outside its mandate."* | `Scope` compiles into the policy graph's only permits — deny-by-default, per tool call, before execution. |
| *"Auditable six months later."* | Hash-chained `WorkflowTrace` on every run and every governed change, using the control plane's ledger scheme. |
| *"Human sign-off on risky actions."* | `HarnessPosture.STRICT`: policy obligations require a synchronous human approval, recorded as `actor="human"` trace entries. |
| *"No model lock-in."* | One LLM core, three providers built in (OpenAI / Anthropic / OpenRouter → any model), pluggable gateways via entry points. |
| *"No vendor lock-in on tooling."* | Agents export to OpenCode's open format; skills are markdown; graphs are JSON; everything works air-gapped. |
| *"Our security team reviews everything."* | Lean core deps (httpx/pydantic/aiofiles), `pip-audit` gated CI, `py.typed`, SECURITY.md, no phone-home. |

---

## Install

```bash
pip install vouchstone-sdk            # lean core — KG pillar works fully offline
```

| Extra | Enables |
|---|---|
| `llm-openai` | OpenAI + OpenRouter providers, semantic-memory embeddings, LLM extraction/enrichment |
| `llm-anthropic` | Anthropic provider, `ClaudeEngineAdapter` |
| `redis` | Working memory on Redis |
| `vector` | Semantic memory on ChromaDB |
| `graph` | Procedural memory on Neo4j / Apache AGE |
| `otel` | OpenTelemetry spans on `Agent.process()` / `Forge.request_change()` |
| `all` | Everything above |

Requires Python 3.10+. **A control plane is optional**: the KG pillar, the
harness, Forge, evals, and the OpenCode bridge all run standalone/air-gapped;
connect a control plane (self-hosted or Vouchstone cloud) for hosted memory,
the Vault, and team-wide governance — see
[Standalone vs. Enterprise](#standalone-oss-vs-enterprise-platform).

---

## Quick Start

The two pillars, end to end: **graph** first, then the **governed harness**
that runs on top of it — the same order as [The two pillars](#the-two-pillars) above.

### 1. Build the graph in Python

The CLI walkthrough above (`vouchstone kg build/verify/agents`) maps 1:1 onto
these calls — reach for the Python API when you want the artifact or the
proposed candidates in-process instead of piping JSON between commands:

```python
from vouchstone_sdk import (
    build_codebase_artifact, verify_artifact, propose_agents_from_artifact,
)

artifact = build_codebase_artifact("./your-repo")
assert verify_artifact(artifact).valid          # tamper-evident hash chain

for candidate in propose_agents_from_artifact(artifact):
    print(candidate.name, "->", candidate.role, "scoped to", candidate.scoped_domains)
    # AgentConfig(**candidate.to_agent_config_kwargs()) is ready to hand to
    # HarnessAgent below, with the boundary already attached.
```

### 2. Run the governed harness, end to end

```python
import asyncio
from vouchstone_sdk import (
    AgentConfig, HarnessAgent, HarnessPosture, Scope, ToolRegistry, Message,
)

def lookup_invoice(invoice_id: str) -> dict:
    """Look up an invoice by id."""
    return {"invoice_id": invoice_id, "amount": 1200}

tools = ToolRegistry()
tools.register(lookup_invoice)          # JSON schema derived from the signature

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

Any tool outside the scope is denied *before* execution and the denial is
both hash-chained and returned to the model. Swap the model string for any
provider — nothing else changes.

### 3. Edit your agents in OpenCode

```bash
# scaffold a full workspace: agents (scoped permissions), skills,
# Vouchstone MCP server wiring, and slash-commands
vouchstone opencode init --from-kg kg.json

# ... edit .opencode/agents/finance-specialist.md in OpenCode ...

# import the edit back through the governance gate (Forge CompatibilityGate
# + signed trace; agent-definition edits carry dual-signoff obligations)
vouchstone opencode import-agent .opencode/agents/finance-specialist.md \
    --previous backups/finance-specialist.md --governed
```

---

### 4. Connect to the Control Plane

```python
from vouchstone_sdk import VouchstoneClient

async with VouchstoneClient(
    api_key="your-api-key",
    control_plane_url="https://your-control-plane-host.example.com",  # required, no default
    tenant_id="your-tenant-id",
) as client:
    # List all agents in your tenant
    agents = await client.list_agents()

    # Get a specific agent definition
    agent = await client.get_agent("agent-123")

    # Report metrics back to control plane
    await client.report_metrics({
        "agent_id": "agent-123",
        "requests": 42,
        "avg_latency_ms": 320,
    })

    # Data plane heartbeat (keeps the control plane informed)
    await client.heartbeat(
        runtime_version="1.0.0",
        pod_count=3,
        queue_depth=12,
        last_seq=1500,
        runtime_token="your-runtime-token",
    )
```

### 5. Use the Memory Pipeline directly

```python
from vouchstone_sdk import MemoryPipeline, Entity, Skill

pipeline = MemoryPipeline(
    agent_id="agent-123",
    redis_url="redis://localhost:6379",
    vector_db_url="http://localhost:8002",
    graph_db_url="bolt://localhost:7687",
)
await pipeline.initialize()

# Before each turn — gather context from all 5 layers
context = await pipeline.prepare_context(
    session_id="sess-abc",
    user_input="How should we handle the CDC replication?"
)

# After each turn — persist to episodic + queue async extraction
result = await pipeline.process_turn(
    session_id="sess-abc",
    turn_number=1,
    user_input="How should we handle the CDC replication?",
    agent_response="I recommend Debezium for CDC with Kafka...",
    tools_used=["search", "knowledge_base"],
    tokens_in=120,
    tokens_out=85,
    latency_ms=1200,
    success=True,
)

# Upsert a semantic entity
await pipeline.semantic.upsert_entity("agent-123", Entity(
    id="e1", entity_type="technology", entity_key="Debezium",
    attributes={"category": "CDC", "use_case": "real-time replication"},
    confidence=0.95,
))

# Register a procedural skill
await pipeline.procedural.register_skill("agent-123", Skill(
    id="s1", name="cdc_setup",
    description="Set up CDC replication pipeline",
    steps=["Analyse source schema", "Configure Debezium connector", "Validate lag"],
    tools_required=["schema_analyzer", "kafka_admin"],
))

# Run meta-memory maintenance (decay, dedup, compress)
report = await pipeline.run_maintenance()

# End session (clears working memory)
await pipeline.end_session("sess-abc")
await pipeline.close()
```

### 6. Build a fully custom Agent (lower-level)

`HarnessAgent` above (step 2) is the recommended path — a governed tool
loop, scope enforcement, and a hash-chained trace, out of the box. Subclass
`Agent` directly instead when you need your own tool-use loop with none of
that scaffolding:

`Agent` itself has no built-in LLM client (that's exactly what
`HarnessAgent` adds) — bring your own call, e.g. via the same
provider-agnostic `resolve_provider` the harness uses internally:

```python
from vouchstone_sdk import (
    Agent, AgentConfig, Message, AgentResponse, MemoryContext, resolve_provider,
)

class DataMigrationAgent(Agent):
    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        # context.working_memory   — current session turns
        # context.episodic_context — past session traces
        # context.semantic_entities — known entities (tech, people, systems)
        # context.procedural_skills — learned procedures
        # context.scratchpad       — per-session key-value store

        provider, model_id = resolve_provider(self.config.model)
        result = await provider.chat(
            model=model_id,
            system=f"You are {self.config.name}.",
            messages=[{"role": "user", "content": message.content}],
        )
        return AgentResponse(content=result.content)

config = AgentConfig(
    name="Data Migration Agent",
    model="claude-sonnet-4-6",
    system_prompt="You help enterprises migrate data between systems.",
)

agent = DataMigrationAgent(config)
await agent.initialize(
    agent_id="agent-123",
    redis_url="redis://localhost:6379",
    vector_db_url="http://localhost:8002",
    graph_db_url="bolt://localhost:7687",
)

session = agent.start_session()
response = await agent.process(Message(content="Migrate PostgreSQL to Snowflake"))
print(response.content)

await agent.end_session()
await agent.close()
```

---

## Enterprise workflows

Concrete flows enterprises run with this SDK today:

1. **Codebase onboarding** — `vouchstone kg build` a 200k-LOC repo into a
   signed graph; commit it; `kg agents` proposes scoped specialists;
   `opencode init --from-kg` gives every team an editable, governed agent
   workspace with live MCP access to the graph.
2. **AP-invoice automation** — a `HarnessAgent` scoped to
   `domains=["finance"]` with ERP tools as its only permits; STRICT posture
   routes flagged actions to a human approver; the trace is the audit
   evidence.
3. **Slack/Jira knowledge capture** — `build_source_artifact()` turns live
   sources into the same signed artifact format; `seed_pipeline_from_artifact`
   grounds any agent's semantic memory in it; `kg diff` shows exactly what
   changed between syncs.
4. **Governed code customization** — Forge (engine → compatibility gate →
   sandbox → signed trace) with OpenCode as the default engine; the
   deterministic Transformation Engine replays past decisions bit-for-bit
   (`replay_and_verify`).
5. **Continuous quality** — the eval harness scores agents per case;
   `vouchstone opencode optimize-agent` drives the control plane's
   Optimization Studio (DSPy) against a persona prompt you just edited.

---

## Architecture

```
YOUR AGENT CODE (this SDK)
    |
    v
+-------------------+          +-------------------+
|   DATA PLANE      |  <--->   |   CONTROL PLANE   |
|                   |          |                   |
| Agent Runtime     |          | Dashboard (Next.js)|
| Working Memory    |  sync    | API (FastAPI)     |
|   (Redis)         |  ---->>  | Control plane     |
| Semantic Memory   |          | Stripe Billing    |
|   (ChromaDB)      |          | Action Gateway    |
| Procedural Memory |          | Signed Ledger     |
|   (Neo4j)         |          | Evals / Optimize  |
+-------------------+          +-------------------+
```

### 5-Layer Memory Stack

Each agent has access to a biologically-inspired persistent memory architecture:

| Layer | Name | Storage | Purpose | Lifecycle |
|-------|------|---------|---------|-----------|
| 1 | **Working** | Redis | Current turn context window | Resets per session |
| 2 | **Episodic** | Control plane API (in-process list when offline) | Turn-by-turn traces with importance scoring | Append-only, 90-day retention |
| 3 | **Semantic** | ChromaDB | Entity knowledge graph (people, tech, systems) | Upsert with merge on collision |
| 4 | **Procedural** | Neo4j | Learned skills as versioned DAG with success rates | Version-bumped on update |
| 5 | **Meta** | Control Plane | Decay, dedup, compress, archive, forget | Scheduled maintenance |

---

## SDK Components

### `Agent` (Base Class)

Subclass `Agent` and implement `run()`. The base class handles:
- Session management (`start_session`, `end_session`)
- Memory context preparation (automatic before each turn)
- Post-turn persistence (automatic after each turn)
- `register_tool(name, func, description)` — a bare `dict[str, Callable]`
  your own `run()` can consult; it derives no JSON schema and has no
  dispatch loop. For real schema-derivation + policy-gated dispatch, use
  `ToolRegistry` with `HarnessAgent` (above) instead.

### `AgentConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | required | Agent display name |
| `model` | str | `claude-sonnet-4-6` | LLM model identifier (any provider — see `resolve_provider`) |
| `temperature` | float | `0.7` | Sampling temperature |
| `max_tokens` | int | `4096` | Max output tokens |
| `system_prompt` | str | `None` | System prompt template |
| `working_memory` | bool | `True` | Enable Layer 1 |
| `semantic_memory` | bool | `True` | Enable Layer 3 |
| `episodic_memory` | bool | `True` | Enable Layer 2 |
| `procedural_memory` | bool | `True` | Enable Layer 4 |
| `meta_memory` | bool | `True` | Enable Layer 5 |
| `embedding_model` | str | `text-embedding-3-small` | Embedding model for vector search |
| `memory_retention_days` | int | `90` | Episodic/working-memory trim horizon |
| `scoped_subgraph` | list[str] \| None | `None` | KG-boundary: entity-type/skill-tag allowlist for this agent's memory queries (see [`Scope`](#2-run-the-governed-harness-end-to-end) for the enforced-at-runtime version used by `HarnessAgent`) |
| `scoped_domains` | list[str] \| None | `None` | KG-boundary: `kg_domains` registry slug allowlist (e.g. `["finance"]`) |

### `vouchstone_sdk.kg` — the Knowledge Graph pillar

| Function | Description |
|---|---|
| `build_codebase_artifact(root, *, previous=None)` | Deterministic (no LLM) `KGArtifact` from a directory of Python source, via stdlib `ast`. Pass `previous` for an incremental rebuild — unchanged files are carried over, not re-parsed. |
| `build_source_artifact(ingester, since, *, connect=True)` | Same signed-artifact format, built from a live `BaseIngester` (Slack/Jira/Confluence/GitHub/Meetings) instead of a directory. |
| `verify_artifact(artifact)` | Re-walks the hash chain; returns `VerifyResult(valid, reason)` — air-gap friendly, no network call. |
| `diff_artifacts(old, new)` | File- and entity-level `ArtifactDiff` (added/removed/changed on both axes). |
| `propose_agents_from_artifact(artifact, *, max_candidates=5, min_entities=3)` | Deterministic agent discovery: one `AgentCandidate` per dominant domain, each with `scoped_domains`/`scoped_subgraph` already populated from the graph and a `to_agent_config_kwargs()` ready for `AgentConfig(**...)`. |
| `semantic_enrich(artifact, *, model="gpt-4o-mini")` | Optional LLM pass that adds module-level summaries to an existing artifact (`llm-openai` extra). |
| `KGArtifact.save(path)` / `KGArtifact.load(path)` | The artifact is plain JSON — commit it, diff it in a PR, `load()` it back. |

`vouchstone kg build/verify/diff/agents` (the CLI shown in [The two pillars](#the-two-pillars)) is a thin wrapper over exactly these functions — nothing the CLI does is unavailable from Python.

### `vouchstone_sdk.llm` — provider-agnostic LLM core

One async chat interface, three built-in providers, selected entirely by the model string:

```python
from vouchstone_sdk import resolve_provider   # also: OpenAIProvider, AnthropicProvider, OpenRouterProvider, LLM_PROVIDERS

provider, model_id = resolve_provider("openrouter/anthropic/claude-sonnet-4-6")
# "claude-*" -> Anthropic, "openai/..." -> OpenAI, anything else -> OpenAI as-is
response = await provider.chat(model=model_id, messages=[...], tools=[...])
```

`OpenRouterProvider` is `OpenAIProvider` pointed at `https://openrouter.ai/api/v1`
with `OPENROUTER_API_KEY` — any model on the OpenRouter catalog, no separate
SDK. `LLM_PROVIDERS` is a plugin registry (same `entry_points` mechanism as
`EVAL_GRADERS`/`ENGINE_ADAPTERS`/`EXTRACTION_STRATEGIES`) so a private
gateway can be registered without touching this package.

### `HarnessAgent`, `Scope`, `ToolRegistry`, `HarnessPosture` — the Harness pillar

| Type | Description |
|---|---|
| `Scope(domains=None, entity_types=None, tags=None, allowed_tools=None, namespace=None)` | The enforced KG boundary. `to_policy_graph()` compiles `allowed_tools` into a deny-by-default `PolicyGraph`'s *only* permits; `memory_kwargs()` feeds `AgentConfig.scoped_domains`/`scoped_subgraph`; `namespace` isolates memory keys per scope. |
| `ToolRegistry()` | `.register(func, *, name=None, description=None)` derives a JSON-schema `parameters` block from the function's signature + type hints via `inspect`. `.dispatch(name, arguments)` calls it (awaiting if async) and returns a string result. |
| `HarnessAgent(config, *, tools=None, scope=None, policy_graph=None, posture=HarnessPosture.AUTO, approval_callback=None, trace=None, provider=None, max_iterations=8)` | Concrete `Agent` subclass: runs the LLM tool-use loop, evaluates every tool call against the policy graph (explicit `policy_graph` > `scope.to_policy_graph()` > deny-all) *before* executing it, and appends a `WorkflowTrace` entry for every turn and every tool call's decision/outcome. |
| `HarnessPosture.AUTO` / `.STRICT` | `AUTO`: obligations are logged and traced, execution proceeds. `STRICT`: any obligation on a permitted call blocks on `approval_callback(request, decision) -> bool`; no callback means deny. |
| `CommandPolicy(allow_patterns, deny_patterns)` + `make_shell_tool(policy)` | A ready-made governed shell tool: deny-by-default regex allowlist, deny patterns override; a rejected command returns `"DENIED: ..."` to the model instead of running. |

A `HarnessAgent` built with neither `scope` nor `policy_graph` has an empty
policy graph and therefore cannot execute *any* tool — deny-by-default has
no implicit escape hatch.

### `vouchstone_sdk.opencode` — the OpenCode bridge

| Function | Description |
|---|---|
| `export_agent(config, workspace, *, scope=None, posture=HarnessPosture.AUTO, role=None)` | Writes `.opencode/agents/<name>.md`: frontmatter (`model`, `temperature`, `permission` map) derived from `derive_permissions(scope, posture)`, body = persona prompt + KG-boundary preamble. |
| `import_agent(path)` | Parses an edited `.md` back into `(AgentConfig kwargs, OpenCodeAgentSpec)`. Unsupported frontmatter keys raise `ValueError` rather than silently dropping data. |
| `governed_import(path, *, previous_markdown=None)` | The same parse, but routed through Forge's `CompatibilityGate` against `agent_definition_policy_graph()` first, with a signed `WorkflowTrace` entry recording the decision — the path CLI's `--governed` flag and CI use. Returns `(kwargs \| None, GateResult, WorkflowTrace)`; a denied or unparseable edit returns `kwargs=None`. |
| `diff_agent_markdown(old, new, name)` | Unified diff between two agent markdown revisions, for `--preview` and audit trails. |
| `export_skill(skill, workspace)` / `copy_skill(procedural_memory, name, *, from_agent, to_agent)` | Skill runbooks as `.opencode/skills/<name>/SKILL.md`; `copy_skill` clones a skill between agents through `ProceduralMemory` with a fresh execution track record for the recipient. |
| `init_workspace(workspace, *, agents=(), skills=(), posture=HarnessPosture.AUTO)` | Scaffolds a full `.opencode/` tree: every agent exported, skills, `opencode.json` pre-wired with the Vouchstone MCP server, and slash-commands (`verify-kg`, `run-evals`, `forge-change`). What `vouchstone opencode init` calls. |

Every agent-definition sync-back (`governed_import`, and `init_workspace`'s
generated commands) goes through the same Forge gate as any other governed
change — OpenCode edits are not a side door around policy.

### `VaultClient`

Async HTTP client for the Document Vault — the enterprise moderation layer:

| Method | Description |
|--------|-------------|
| `list_vaults()` | List all vaults in your tenant |
| `create_vault(name, description)` | Create a new vault |
| `get_vault(vault_id)` | Fetch vault details |
| `upload_files(vault_id, files, *, layer, path_prefix)` | Upload files — each entry is a dict with `filename`, `content` (bytes), optional `content_type` |
| `list_tree(vault_id, *, layer, path)` | List documents in a vault layer as a file tree |
| `get_document(vault_id, document_id, *, version)` | Fetch a document (optionally a historical commit SHA) |
| `search(vault_id, query, *, layer, limit)` | Full-text / semantic search within a vault layer |
| `approve(vault_id, document_ids)` | Approve documents (promotes Workspace → Canonical) |
| `reject(vault_id, document_ids, *, reason)` | Reject documents from the moderation queue |
| `ingest(vault_id, *, target)` | Ingest Canonical documents into `"kg"`, `"wiki"`, `"brain"`, or `"all"` |
| `set_autopilot(vault_id, *, enabled, source_id)` | Toggle auto-pilot ingestion for a vault or one source |

```python
from vouchstone_sdk import VaultClient

async with VaultClient(
    api_key="your-api-key",
    control_plane_url="https://your-control-plane-host.example.com",  # required, no default
    tenant_id="your-tenant-id",
) as vault:
    # Upload files — text extraction happens server-side
    result = await vault.upload_files("vault-id", [
        {"filename": "report.pdf", "content": open("report.pdf", "rb").read(),
         "content_type": "application/pdf"},
        {"filename": "data.csv", "content": open("data.csv", "rb").read(),
         "content_type": "text/csv"},
    ])
    doc_ids = [d["id"] for d in result["documents"]]

    # Browse vault contents
    tree = await vault.list_tree("vault-id", layer="workspace")

    # Moderate: approve documents for downstream use
    await vault.approve("vault-id", doc_ids)

    # Ingest approved (Canonical) docs into Knowledge Graph + Wiki + Brain
    await vault.ingest("vault-id", target="all")

    # Enable auto-pilot for a connector source
    await vault.set_autopilot("vault-id", enabled=True, source_id="slack")
```

### `DomainClient`

Async HTTP client for the Knowledge-Graph domain builder — talks to the real
`/api/v1/ckg/domains`, `/ckg/sub-graphs`, and `/ckg/extract` endpoints
(`app/services/kg_domains.py`, `sub_graphs.py`, `domain_classifier.py`).
Domains are auto-taxonomy: the extraction pipeline classifies each promoted
node into domain slugs itself, and any new slug the LLM proposes is
persisted as a real registry row the instant it's proposed — there's no
separate "define a domain" step:

| Method | Description |
|--------|-------------|
| `list_domains()` | The tenant's full kg_domains registry tree |
| `curate_domain(slug, name=..., parent_slug=...)` | Rename/re-describe/re-parent a domain's display metadata |
| `classify()` | Backfill auto-taxonomy classification for unclassified nodes |
| `list_sub_graphs()` | Every domain with at least one real node, with rollup counts + a computed health score |
| `get_sub_graph(slug)` | Nodes + edges for one domain (includes descendants' nodes for a department rollup) |
| `extract_documents(documents)` | Run the N-pass extraction pipeline over raw documents |
| `get_extraction(job_id)` / `wait_for_extraction(job_id)` | Poll an extraction job to completion |

```python
from vouchstone_sdk import DomainClient

async with DomainClient(
    api_key="your-api-key",
    control_plane_url="https://your-control-plane-host.example.com",  # required, no default
    tenant_id="your-tenant-id",
) as dc:
    # extract -- run extraction over raw documents (or use
    # VaultClient.ingest(vault_id, target="kg") for vault-moderated content)
    job = await dc.extract_documents([
        {"filename": "vendor-contract.md", "content": "..."},
    ])
    job = await dc.wait_for_extraction(job.id)

    # domain KG -- backfill classification, then browse the result
    await dc.classify()
    domains = await dc.list_domains()
    sub_graphs = await dc.list_sub_graphs()
    finance_kg = await dc.get_sub_graph("finance")

    # curate a domain's display metadata (never touches node classifications)
    await dc.curate_domain("finance", name="Finance & Accounting")
```

### `VouchstoneClient`

Async HTTP client for the control plane API:

| Method | Description |
|--------|-------------|
| `list_agents()` | List all agents in your tenant |
| `get_agent(id)` | Fetch a specific agent definition |
| `report_metrics(data)` | Push runtime metrics to control plane |
| `report_status(agent_id, status)` | Report agent health status |
| `heartbeat(...)` | Data plane heartbeat (required for sync) |
| `replay_ledger(entries)` | Bulk replay audit ledger entries |
| `fetch_agent_spec(...)` | Fetch latest agent specifications |

### `MemoryPipeline`

Orchestrates all 5 memory layers:

| Method | Description |
|--------|-------------|
| `prepare_context(session_id, input)` | Gather context from all layers before a turn |
| `process_turn(...)` | Persist turn result to episodic + queue extraction |
| `run_reflection(session_id)` | Discover new skills from episodic traces |
| `run_maintenance()` | Run meta-memory (decay, dedup, compress) |
| `get_snapshot()` | Full memory snapshot across all layers |
| `end_session(session_id)` | Clear working memory for a session |

### Key Types

| Type | Description |
|------|-------------|
| `Message` | Input message with content, role, metadata |
| `AgentResponse` | Response with content, decisions, tool calls, usage |
| `MemoryContext` | Full context from all 5 layers (passed to `run()`) |
| `EpisodicTrace` | A single turn trace with importance score |
| `Entity` | A semantic entity (person, technology, system, etc.) |
| `Skill` | A procedural skill with steps, tools, success rate |
| `HealthReport` | Memory health stats and recommendations |
| `Domain` | A kg_domains registry row (slug, name, hierarchy, display metadata) |
| `SubGraphSummary` | A domain card with rollup node/edge counts and a computed health score |
| `SubGraph` | Nodes + edges for one domain |
| `ExtractionJob` | A CKG extraction job's status and progress |
| `ClassifyResult` | Result of a domain-classification backfill pass |

### `EntityGraph`, `PolicyGraph`, `WorkflowTrace` — the three-compartment pattern

Every accountable-agent use case (AP-invoice matching, compliance evidence,
data migration, ...) decomposes into the same three compartments: domain
entities/edges, a stable policy ruleset, and an append-only, verifiable
record of what happened. Define these once per use case — no SDK code
changes needed between use cases.

```python
from datetime import datetime, timezone
from vouchstone_sdk import EntityGraph, PolicyGraph, Policy, WorkflowTrace, Entity

# 1. EntityGraph — what the agent knows
graph = EntityGraph()
graph.add_entity(Entity(id="inv-1", entity_type="invoice", entity_key="INV-001",
                         attributes={"amount": 1200.0}, confidence=1.0,
                         source_trace_id=None, created_at=datetime.now(timezone.utc)))

# 2. PolicyGraph — what the agent is allowed to do (deny-by-default)
policy = PolicyGraph()
policy.add_policy(Policy(
    name="auto-approve small invoices", effect="permit",
    action={"eq": "invoice.approve"},
    conditions=[{"path": "resource.amount", "op": "lt", "value": 5000}],
    obligations=["log_to_audit"],
))
decision = policy.evaluate(
    principal={"agent_id": "ap-agent-1"}, action="invoice.approve",
    resource={"amount": 1200.0},
)  # PolicyDecision(allow=True, obligations=["log_to_audit"], ...)

# 3. WorkflowTrace — what actually happened, hash-chained and verifiable
trace = WorkflowTrace()
trace.append("invoice.approved", {"invoice_id": "inv-1", "decision": decision.allow})
assert trace.verify_chain()
```

`WorkflowTrace` uses the exact same canonical-JSON + SHA-256 hash-chaining
algorithm as the control plane's signed ledger
(`app/services/ledger_signing.py`) — a hash computed locally is
byte-identical to one computed by the hosted ledger for the same input,
which is what makes an offline trace independently verifiable rather than
"trust the SDK's math."

### `Forge` — framework-agnostic agent customization

Forge does not compete with Google ADK, the Claude Agent SDK, or your own
LangChain/CrewAI setup at the tool-use-loop layer — plug any of those in
via `EngineAdapter`. What Forge owns: every proposed change, regardless of
which engine produced it, passes through the same compatibility gate
(structural validity + `PolicyGraph` evaluation) and gets a signed,
hash-chained `WorkflowTrace` entry recording the decision.

```python
from vouchstone_sdk import (
    Forge, ClaudeEngineAdapter, CompatibilityGate, PolicyGraph, Policy,
    SubprocessSandboxRunner, WorkflowTrace,
)

policies = PolicyGraph()
policies.add_policy(Policy(
    name="permit handler changes", effect="permit",
    action={"eq": "forge.apply_change"},
    conditions=[{"path": "resource.file_path", "op": "startswith", "value": "handlers/"}],
))

forge = Forge(
    gate=CompatibilityGate(policies),
    sandbox_runner=SubprocessSandboxRunner(),  # reference runner -- see its docstring
    trace=WorkflowTrace(),
)

result = await forge.request_change(
    "add input validation to the webhook handler",
    context={"files": {"handlers/webhook.py": open("handlers/webhook.py").read()}},
    engine=ClaudeEngineAdapter(),  # or your own EngineAdapter subclass
)

if result.passed:
    for change in result.diff.changes:
        print(change.file_path, "->", len(change.new_content), "bytes, ready to apply")
else:
    print("blocked:", result.gate_result.reason)
```

`SubprocessSandboxRunner` is a real, working reference implementation
(actually executes proposed Python files, not just a syntax check) but is
explicitly **not isolated** — production deployments must supply their
own container-isolated `SandboxRunner`. See the class docstring.

### `TemplateEngineAdapter` — the Deterministic Transformation Engine

LLM code generation itself can't be made deterministic — sampling is
inherent. What's genuinely reproducible: for common, high-risk
customizations (threshold changes, policy rule additions, ...), select
and parameterize an already-verified-safe template instead of generating
free-form code. Same template + same params always renders
byte-identical output, and `replay_and_verify()` proves that against a
past signed decision, not just claims it.

```python
from vouchstone_sdk import (
    Forge, TemplateEngineAdapter, default_template_library,
    CompatibilityGate, PolicyGraph, Policy, WorkflowTrace, replay_and_verify,
)

policies = PolicyGraph()
policies.add_policy(Policy(name="permit config changes", effect="permit", action={"eq": "forge.apply_change"}))
gate = CompatibilityGate(policies)
trace = WorkflowTrace()

forge = Forge(gate=gate, trace=trace, sandbox_runner=None)
engine = TemplateEngineAdapter(default_template_library())  # optionally: fallback_engine=ClaudeEngineAdapter()

result = await forge.request_change(
    "raise the approval threshold",
    {"template_params": {"threshold_usd": 7500}},
    engine=engine, run_sandbox=False,
)

# Later -- e.g. loading the signed entry back from a persisted ledger --
# prove the decision is still reproducible:
replay = replay_and_verify(result.trace_entry.payload, engine.library, gate)
assert replay.reproducible
```

An instruction that matches no template falls through to
`fallback_engine` (any `EngineAdapter`, e.g. `ClaudeEngineAdapter`) for
genuinely novel changes — and the resulting `Diff.metadata["templated"]`
is `False`, an explicit signal that this change has nothing pinned to
replay against and should get heavier review than a templated one.

### OpenTelemetry observability

`Agent.process()` and `Forge.request_change()` are instrumented with real
OpenTelemetry spans — `opentelemetry-api`/`opentelemetry-sdk` are the
optional `otel` extra, not a core dependency, so the SDK works identically
(spans become real no-ops) for a customer who doesn't install or
configure OTel at all.

```python
from vouchstone_sdk import configure_telemetry

# Wire up any real OTel exporter (OTLP, Jaeger, console, ...). batch=True
# (default) uses BatchSpanProcessor for production; pass batch=False only
# if you need synchronous export (e.g. tests inspecting spans immediately).
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
configure_telemetry(service_name="my-agent-fleet", exporter=OTLPSpanExporter())
```

If your host application already configures its own OTel `TracerProvider`
(e.g. via `opentelemetry-instrument` or your own bootstrap code), skip
`configure_telemetry()` entirely — the SDK's spans register against
whatever provider is globally set, same as any other OTel-instrumented
library. Spans emitted: `vouchstone.agent.process` (agent name/ID,
session ID, turn number) and `vouchstone.forge.request_change` (engine
name, gate allow/deny, pass/fail outcome), both with exceptions recorded
and span status set to `ERROR` on failure.

### Local eval harness

Runs entirely against your own agent instance — no hosted control plane,
no network dependency. Each `EvalCase` genuinely executes through
`Agent.process()` (the real memory pipeline, the real `run()`
implementation), not a mock of expected behavior, and gets its own
session by default so cases can't leak state into each other.

```python
from vouchstone_sdk import EvalCase, EvalSuite, run_eval_suite

suite = EvalSuite(name="ap-invoice-agent").add(
    EvalCase(name="approves-under-threshold", input_content="Invoice #4521, $2,000, matches PO", expected_output="approved")
).add(
    EvalCase(name="escalates-over-threshold", input_content="Invoice #4522, $50,000, matches PO", expected_output="escalate")
)

report = await run_eval_suite(my_agent, suite)
print(f"{report.passed}/{report.total} passed, avg score {report.average_score}")
```

The default grader is a plain substring match — good enough for a smoke
test, not for grading nuance. Pass a custom grader (exact match,
structural comparison, or an LLM-judge grader you write) either per-case
via `EvalCase(grader=...)` or suite-wide via `run_eval_suite(..., grader=...)`.

### Plugin model

Real Python `entry_points` discovery — the same mechanism pytest and
flake8 use — for third-party engines, extraction strategies, and eval
graders. A separately installable package declares an entry point in its
own `pyproject.toml`:

```toml
[project.entry-points."vouchstone.eval_graders"]
my_grader = "my_package.graders:strict_json_grader"
```

and it's discoverable with zero code on Vouchstone's side:

```python
from vouchstone_sdk import EVAL_GRADERS, ENGINE_ADAPTERS, EXTRACTION_STRATEGIES

EVAL_GRADERS.names()          # ["default", "exact_match", "my_grader"] once installed
grader = EVAL_GRADERS.get("my_grader")
```

For a single script or notebook that doesn't want to publish a whole
package, register in-process instead: `EVAL_GRADERS.register("my_grader", my_fn)`
— manual registrations take precedence over discovered ones of the same
name. `ENGINE_ADAPTERS` ships pre-registered with `echo`, `claude`,
`template`, and `opencode` (the built-in `EngineAdapter`s from the Forge
sections above and the OpenCode bridge); `EXTRACTION_STRATEGIES` ships
`llm` and `deterministic` (used by `vouchstone_sdk.ingestion`) and is open
for a customer's own local extraction logic against
`EntityGraph`/`LocalKGStore`.
A broken entry point raises `PluginLoadError` naming the failing plugin
rather than being silently dropped from the list.

---

### Enterprise ingestion — `vouchstone_sdk.ingestion`

Five real source ingesters feed raw enterprise activity into the Knowledge
Graph, each speaking its vendor's actual API (no stubs):

| Ingester | Source | Fetches |
|---|---|---|
| `SlackIngester` | Slack Web API | channel history incl. thread replies |
| `JiraIngester` | Jira Cloud REST v3 | issues via JQL, walks ADF rich text |
| `ConfluenceIngester` | Confluence REST | spaces + paginated page content, HTML→text |
| `GitHubIngester` | GitHub REST | PRs (+files/reviews), commits, issues |
| `MeetingIngester` | Zoom / MS Graph / Google Drive | recordings + meeting transcripts |

The shared `BaseIngester` pipeline extracts entities and relationships with an
LLM (requires the `llm-openai` extra), generates embeddings, and writes to
ChromaDB + Neo4j. `IngestionPipeline` orchestrates multi-source syncs with
cross-source dedup.

```python
from vouchstone_sdk.ingestion import SlackIngester
from vouchstone_sdk.ingestion.pipeline import IngestionPipeline

pipeline = IngestionPipeline()
pipeline.register(SlackIngester(bot_token="xoxb-...", chromadb_url="http://chroma:8000"))
statuses = await pipeline.sync_all()
```

Subclass `BaseIngester` (implement `connect()` and `fetch_raw()`) to add a
source; the extraction/dedup/KG-write pipeline is inherited.

---

## Data Plane Sync

The SDK supports bidirectional sync with the Vouchstone control plane:

```python
# Heartbeat — call every 30s to keep the control plane informed
await client.heartbeat(
    runtime_version="1.0.0",
    pod_count=3,
    queue_depth=12,
    last_seq=1500,
    runtime_token="your-runtime-token",
)

# Fetch latest agent specs (new deployments, config changes)
specs = await client.fetch_agent_spec(since=last_sync_timestamp)

# Replay audit entries from data plane to control plane
await client.replay_ledger(entries=[
    {"action": "agent_executed", "agent_id": "...", "timestamp": "..."},
])
```

---

## Environment Variables

Most SDK classes take their configuration as **explicit constructor
kwargs** (`VouchstoneClient(api_key=..., control_plane_url=..., tenant_id=...)`,
`MemoryPipeline(redis_url=..., vector_db_url=..., graph_db_url=...)`) — there
is no implicit env-var fallback for those, on purpose (a library that
silently reads ambient env vars is harder to reason about in a multi-tenant
process). The env vars actually read are narrower:

| Variable | Read by | Purpose |
|----------|---------|---------|
| `OPENROUTER_API_KEY` | `OpenRouterProvider` (used whenever `model="openrouter/..."`) | Auth for any model on OpenRouter's catalog |
| `OPENAI_API_KEY` | The `openai` SDK itself, when `OpenAIProvider`/ingestion's LLM extraction is constructed without an explicit key | Auth for native OpenAI calls and embeddings |
| `ANTHROPIC_API_KEY` | The `anthropic` SDK itself, when `AnthropicProvider`/`ClaudeEngineAdapter` is constructed without an explicit key | Auth for native Anthropic calls |
| `VOUCHSTONE_FORGE_ENGINE` | `describe_forge_engine()` / Forge's default-engine resolution | Selects the default `EngineAdapter` (defaults to `opencode`) |
| `VOUCHSTONE_OPENCODE_PATH` | `OpenCodeEngineAdapter` | Overrides the `opencode` binary path (defaults to `PATH` lookup) |
| `VOUCHSTONE_API_URL` / `VOUCHSTONE_API_KEY` / `VOUCHSTONE_TENANT_ID` | `vouchstone opencode optimize-agent` (CLI only) | Fallback when `--api-url`/`--api-key`/`--tenant-id` aren't passed |

---

## Standalone OSS vs. Enterprise Platform

Everything in this repository is Apache-2.0 and works without a Vouchstone
account. The hosted/enterprise control plane adds the team- and
compliance-grade layer on top of the same primitives:

| Capability | OSS SDK (this repo) | + Vouchstone Enterprise |
|---|---|---|
| Knowledge graph | Signed local artifacts, deterministic + optional LLM pass | Hosted 5-pass LLM extraction pipeline, 69-connector catalog, Document Vault moderation (Raw→Workspace→Canonical), auto-compiled Wiki, Company Brain RAG with cited answers |
| Agent harness | Governed tool loop, scopes, postures, local traces | Action Gateway with Constitution/Authority-Matrix policy, autonomy levels (L0–L4), approval queues, tenant-wide signed ledger with replay |
| Memory | 5 layers with your own Redis/Chroma/Neo4j (or in-process) | Hosted multi-tenant memory with Meta-Memory governance (decay, dedup, compression) run for you |
| Agent discovery | From local artifacts (`kg agents`) | From the live Customer Knowledge Graph (`suggest-from-kg`), with the Strategy Council verifying answers |
| Evals & optimization | Local eval harness | Evals dashboards, cost-per-run billing, DSPy Optimization Studio |
| Operations | You run it | Monitoring, usage billing, SLAs, enterprise support, sovereign/air-gap deployment programs |

The upgrade path is incremental: point `VouchstoneClient` at a control plane
and the same `Agent`/`MemoryPipeline`/`VaultClient` code you wrote against
local backends starts using hosted ones. Talk to
[renu@vouchstone.ai](mailto:renu@vouchstone.ai) or see
[vouchstone.ai/pricing](https://vouchstone.ai/pricing).

---

## Development

```bash
git clone https://github.com/GGChamp85/Vouchstone.git
cd Vouchstone/data-plane/sdk/python

pip install -e ".[all,dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check vouchstone_sdk/

# Type check
mypy

# Known-vulnerability scan
pip-audit
```

This is exactly the gate `.github/workflows/sdk-ci.yml` and `publish-sdk.yml` run — green locally means green in CI.

---

## License

Apache-2.0 — Copyright (c) 2026 Vouchstone LLC. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
