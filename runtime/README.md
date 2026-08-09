# Vouchstone Agent Runtime

Executes agents built with the [Vouchstone Python SDK](../sdk/python/README.md).
Runs one of two ways:

- **Connected** — talks to a live control plane for agent specs, heartbeats,
  and ledger replay (`VOUCHSTONE_CONTROL_PLANE_URL` required).
- **Offline harness** (C4) — initialized once from a signed local bundle,
  then operates with **zero network calls** against a local KG snapshot.
  No control plane needed at all after the initial pull.

## Offline Harness Mode

### Pull a bundle

```bash
python -m src.harness_cli harness pull <agent-id> \
  --tenant-id <tenant-id> \
  --control-plane-url https://your-control-plane-host.example.com \
  --api-key <your-api-key> \
  --out ./my-agent-bundle
```

This calls `GET /api/v1/harness/bundle/{agent_id}` on the control plane,
which assembles and signs a bundle containing:

- The agent's definition (model, memory config, system config)
- A snapshot of the tenant's **promoted** knowledge graph (draft/unreviewed
  extraction output never leaves the control plane in a bundle — see
  `CLAUDE.md`'s Document Vault section on promotion as the trust boundary)
- The pinned SDK version the bundle was built against

...then verifies it locally before declaring success — hash-checked
(canonical-JSON + SHA-256, the same algorithm as the control plane's
signed ledger) and signature-checked (Ed25519 in production; HMAC for
dev/test only).

### Run fully offline

```bash
export VOUCHSTONE_BUNDLE_PATH=./my-agent-bundle
export VOUCHSTONE_TENANT_ID=<tenant-id>
python -m src.main
```

`AgentRuntime.initialize()` detects `BUNDLE_PATH` and calls
`initialize_from_bundle()` instead of connecting to a control plane. It:

1. Re-verifies the bundle's hash and signature (never trusts a bundle
   just because it's on disk).
2. Loads the KG snapshot into an embedded, persistent SQLite store
   (`LocalKGStore`, `local_kg.py`) — the snapshot survives process restarts.
3. Constructs each agent with `local_only=True`, which disables semantic
   memory's vector search entirely rather than falling back to an ambient
   embedded ChromaDB (that fallback still calls out to an embedding
   provider over the network on every upsert/search — not actually
   offline). Falls back to local substring matching instead.
4. Seeds each agent's semantic memory from the KG snapshot via
   `upsert_entity()`, so `MemoryContext.semantic_entities` works exactly
   like the connected path — no code difference in `run()`.
5. Starts zero background tasks — no heartbeat loop, no ledger replay,
   no control-plane client constructed at all.

### Other commands

```bash
python -m src.harness_cli harness verify ./my-agent-bundle   # re-check hash + signature
python -m src.harness_cli harness status ./my-agent-bundle   # print the manifest
python -m src.harness_cli harness sync ./my-agent-bundle \   # re-pull latest, report the diff
  --control-plane-url ... --api-key ...
python -m src.harness_cli harness scan                       # dependency vulnerability scan (C8)
python -m src.harness_cli harness scan --requirement requirements.txt --json
```

`harness push` (git-shaped, compatibility-gated merge-back of local agent
customizations) is intentionally **not yet available** — it depends on the
compatibility gate itself, which ships with Vouchstone Forge. Running it
exits non-zero with an explanation rather than accepting a push with no
real gate behind it.

### CI/CD-native artifact validation (C8)

`verify` and `scan` are both designed as pipeline gates: real, distinct
exit codes, not just human-readable output.

- `harness verify` — `0` valid bundle, `1` invalid (bad hash/signature).
- `harness scan` — `0` clean, `1` known vulnerabilities found, `2` could
  not scan at all (`pip-audit` unavailable). `2` is deliberately distinct
  from `0` — an environment without `pip-audit` installed must never be
  reported as "scanned, clean."

`scan` wraps the real `pip-audit` CLI (`pip install pip-audit`) and
parses its actual JSON output — not a hand-rolled vulnerability list.
Without `--requirement`, it scans the currently active Python
environment; pass a requirements file to scan a specific pinned
dependency set instead — e.g. `harness scan --requirement requirements.txt`
as an advisory (non-blocking) step in your own CI; hard-gating deploys on
it is a policy decision for whoever owns acceptable severity/allowlisting,
not something this command bakes in implicitly.

## Bundle Format

```
<bundle_dir>/
    manifest.json       # BundleManifest -- agent defs, hash, signature
    kg_snapshot.sqlite   # embedded KG snapshot (entities + edges tables)
```

### KG snapshot schema versioning + idempotent sync (C8)

`kg_snapshot.sqlite`'s schema is versioned (`src/local_kg.py`,
`CURRENT_SCHEMA_VERSION`) so a customer's already-pulled bundle on disk
never silently breaks or loses data when opened by newer runtime code.
`LocalKGStore.initialize()` reads the stored version from the snapshot's
`_meta` table and migrates forward in-place through `MIGRATIONS` before
any query runs; opening a snapshot stamped with a version *newer* than
the running code understands raises `SchemaVersionError` outright rather
than reading it partially. `harness status` surfaces the current
snapshot's `kg_schema_version`.

v1 → v2 fixed a real idempotency bug along the way: v1's `edges` table
had no uniqueness constraint, so re-importing the same KG snapshot twice
(e.g. re-running `harness sync` against a bundle whose KG grew)
duplicated every edge that already existed. v2 adds
`UNIQUE(source_id, target_id, edge_type)`, and edge/entity writes
(`add_edge`, `upsert_entity`, `import_entity_graph`) are all upserts —
repeated syncs converge instead of accumulating duplicates. Migrating an
existing v1 snapshot dedupes any edges that already duplicated under the
old schema, keeping the most recently written attributes for each.

See `src/bundle.py` for the manifest schema and verification logic, and
[`sdk/python/vouchstone_sdk/graph.py`](../sdk/python/vouchstone_sdk/graph.py)
for `EntityGraph` — the KG snapshot is a persisted `EntityGraph`, nothing
bundle-specific about its shape.

## Sovereign Deployment Mode (C7b)

For government/defense/EU-sovereignty-mandated deployments where no
external AI-vendor dependency is acceptable at all, inference included —
not just customer data staying local (which the offline harness mode
above already gives you), but the LLM calls themselves never leaving the
deployment's own network.

**Scope note:** turnkey automated vLLM/TGI deployment and benchmarking
open-weight models for production viability are **not** built here —
both need real GPU/infra this environment doesn't have. What's built is
the part that's genuinely verifiable without infra: a hard startup guard
a security reviewer can point at and independently confirm.

```bash
export VOUCHSTONE_SOVEREIGN_MODE=true
export VOUCHSTONE_LOCAL_LLM_BASE_URL=http://localhost:8000/v1  # your own vLLM/TGI endpoint
```

With `VOUCHSTONE_SOVEREIGN_MODE=true`, `AgentRuntime.initialize()` runs
`sovereign.enforce_sovereign_mode()` first, before anything else, and
refuses to start at all (raises `SovereignModeViolation`, not a logged
warning) if either check fails:

1. **Static** — `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` set with no
   `LOCAL_LLM_BASE_URL` override means the SDK would call the real
   hosted endpoint directly.
2. **Dynamic** — a real TCP connect probe (port 443, concurrent across
   hosts) against known external LLM API hosts (`api.openai.com`,
   `api.anthropic.com`, and others — see `KNOWN_EXTERNAL_LLM_HOSTS` in
   `src/sovereign.py`). Proves egress is actually blocked at the
   network/firewall level, independent of what this process has
   configured — a misconfigured or missing firewall rule fails this
   check even if no API key is set anywhere.

Run the reachability check standalone, exactly as a security reviewer
would:

```bash
python -c "from src.sovereign import check_no_external_endpoints_reachable; check_no_external_endpoints_reachable()"
```

## Development

```bash
cd runtime
pip install -r requirements-dev.txt
pytest tests/ -v
```

`requirements.txt` installs `vouchstone-sdk` from PyPI (with the
`redis`/`vector`/`graph` extras this runtime actually uses — see the
comments in that file). To develop against local, unpublished SDK
changes instead, swap that line for an editable install:

```bash
pip uninstall -y vouchstone-sdk
pip install -e "../sdk/python[redis,vector,graph]"
```
