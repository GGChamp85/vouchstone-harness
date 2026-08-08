# Vouchstone SDK

Python SDK for the **Vouchstone Enterprise AI Agent Platform** — build, deploy, and govern production AI agents with persistent memory, full auditability, and enterprise-grade controls.

Vouchstone LLC | [Website](https://vouchstone.ai) | [Docs](https://vouchstone.ai/docs) | [GitHub](https://github.com/GGChamp85/Vouchstone)

---

## What Is Vouchstone?

Vouchstone is the first **Accountable AI Engineering Platform** — a control plane + data plane architecture for enterprises that need AI agents they can trust, audit, and govern. The platform provides:

- **AI Agent Lifecycle** — Create, deploy, monitor, and retire agents through a managed control plane
- **5-Layer Persistent Memory** — Working, Episodic, Semantic, Procedural, and Meta-Memory
- **Document Vault** — 3-layer moderation gateway (Raw/Workspace/Canonical) for all enterprise data
- **Knowledge Platform** — Automated extraction to Knowledge Graph, Wiki, and Company Brain
- **Enterprise Governance** — ABAC policies, RACI matrices, cost governance, shadow mode, compliance packs
- **Multi-Tenant SaaS** — Tenant-isolated data, Stripe billing, SAML/OIDC federation
- **Enterprise ingestion** — 5 shipped source ingesters (Slack, Jira, Confluence, GitHub, Meetings — Zoom/Teams/Google Meet), extensible via `BaseIngester`; the hosted control plane's connector catalog covers 69 sources

This SDK lets you build custom agents that run on the Vouchstone data plane and interact with the control plane APIs.

---

## Install

```bash
pip install vouchstone-sdk
```

### Optional Extras

```bash
# LLM providers — required only for LLM-touching features
# (ClaudeEngineAdapter, semantic-memory embeddings, ingestion extraction)
pip install vouchstone-sdk[llm-openai]
pip install vouchstone-sdk[llm-anthropic]

# Working memory (Redis-backed per-session context)
pip install vouchstone-sdk[redis]

# Semantic memory (ChromaDB vector search)
pip install vouchstone-sdk[vector]

# Procedural memory (Neo4j skill graph)
pip install vouchstone-sdk[graph]

# OpenTelemetry observability (spans on Agent.process() / Forge.request_change())
pip install vouchstone-sdk[otel]

# Everything
pip install vouchstone-sdk[all]
```

### Requirements

- Python 3.10+
- A Vouchstone control plane instance (self-hosted or cloud)
- API key from your Vouchstone tenant

---

## Quick Start

### 1. Build a Custom Agent

```python
from vouchstone_sdk import Agent, AgentConfig, Message, AgentResponse, MemoryContext

class DataMigrationAgent(Agent):
    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        # context.working_memory   — current session turns
        # context.episodic_context — past session traces
        # context.semantic_entities — known entities (tech, people, systems)
        # context.procedural_skills — learned procedures
        # context.scratchpad       — per-session key-value store

        # Your LLM call here
        response = await self.llm.complete(
            system=f"You are {self.config.name}.",
            messages=[{"role": "user", "content": message.content}],
        )
        return AgentResponse(content=response)

config = AgentConfig(
    name="Data Migration Agent",
    model="claude-sonnet-4-20250514",
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

### 2. Connect to the Control Plane

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

### 3. Use the Memory Pipeline Directly

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
|   (ChromaDB)      |          | ABAC / RACI       |
| Procedural Memory |          | Cost Governance   |
|   (Neo4j)         |          | Compliance Packs  |
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
- Tool registration

### `AgentConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | required | Agent display name |
| `model` | str | `claude-sonnet-4-20250514` | LLM model identifier |
| `temperature` | float | `0.7` | Sampling temperature |
| `max_tokens` | int | `4096` | Max output tokens |
| `system_prompt` | str | `None` | System prompt template |
| `working_memory` | bool | `True` | Enable Layer 1 |
| `semantic_memory` | bool | `True` | Enable Layer 3 |
| `episodic_memory` | bool | `True` | Enable Layer 2 |
| `procedural_memory` | bool | `True` | Enable Layer 4 |
| `meta_memory` | bool | `True` | Enable Layer 5 |
| `embedding_model` | str | `text-embedding-3-small` | Embedding model for vector search |

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

EVAL_GRADERS.names()          # e.g. ["default", "my_grader"] once installed
grader = EVAL_GRADERS.get("my_grader")
```

For a single script or notebook that doesn't want to publish a whole
package, register in-process instead: `EVAL_GRADERS.register("my_grader", my_fn)`
— manual registrations take precedence over discovered ones of the same
name. `ENGINE_ADAPTERS` ships pre-registered with `echo`, `claude`, and
`template` (the built-in `EngineAdapter`s from the Forge sections above);
`EXTRACTION_STRATEGIES` starts empty — a pure extension point for a
customer's own local extraction logic against `EntityGraph`/`LocalKGStore`.
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

| Variable | Required | Description |
|----------|----------|-------------|
| `VOUCHSTONE_API_KEY` | Yes | API key from your tenant |
| `VOUCHSTONE_API_URL` | **Yes** | Control plane URL -- required, no default |
| `VOUCHSTONE_TENANT_ID` | Yes | Your tenant identifier |
| `REDIS_URL` | No | Redis URL for working memory |
| `CHROMADB_URL` | No | ChromaDB URL for semantic memory |
| `NEO4J_URL` | No | Neo4j URL for procedural memory |
| `OPENAI_API_KEY` | No | For embedding generation (semantic layer) |

---

## Development

```bash
git clone https://github.com/GGChamp85/Vouchstone.git
cd Vouchstone/data-plane/sdk/python

pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Format
black vouchstone_sdk/

# Type check
mypy vouchstone_sdk/
```

---

## License

Apache-2.0 — Copyright (c) 2026 Vouchstone LLC. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
