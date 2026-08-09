"""End-to-end offline harness tests (C4).

Acceptance criteria: a runtime initialized from a local bundle operates
fully offline -- zero network calls -- against a local KG snapshot.

These tests build a bundle exactly the way the control plane's
/api/v1/harness/bundle/{agent_id} endpoint does (see
control-plane/backend/app/api/v1/endpoints/harness.py), sign it with both
supported schemes (HMAC for the dev/test fallback, Ed25519 for the real
production signing path), then prove the runtime can load and use it with
zero network calls -- not just "no error", but an active check that
httpx.AsyncClient is never even constructed during offline init.
"""
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bundle import (
    BundleManifest, BundleVerificationError, compute_entry_hash, compute_kg_snapshot_hash,
    save_bundle, verify_bundle,
)
from src.config import Settings
from src.harness_cli import _build_kg_snapshot_sqlite
from src.runtime import AgentRuntime

HMAC_SECRET = "test-shared-secret"


def _sample_entities():
    return [
        {"id": "ent-1", "entity_type": "system", "entity_key": "Postgres primary",
         "attributes": {"engine": "postgres"}, "confidence": 0.95,
         "source_trace_id": None, "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "ent-2", "entity_type": "data", "entity_key": "shipments table",
         "attributes": {"row_count": 50000}, "confidence": 0.9,
         "source_trace_id": None, "created_at": "2026-01-01T00:00:00+00:00"},
    ]


def _sample_edges():
    return [{"source_id": "ent-2", "target_id": "ent-1", "edge_type": "hosted_on", "attributes": {}}]


def _build_manifest(tenant_id: str, agent_id: str, sign_hmac: bool = True, ed25519_private_key=None) -> BundleManifest:
    entities = _sample_entities()
    edges = _sample_edges()
    agent_def = {
        "id": agent_id, "name": "Schema Mapper", "status": "active",
        "memory_config": {"semantic": {"enabled": True}, "episodic": {"enabled": True}, "procedural": {"enabled": False}},
        "config_schema": {"model": "gpt-4-turbo-preview"},
    }
    content = {
        "bundle_ref": f"{agent_id}-test", "tenant_id": tenant_id, "agents": [agent_def],
        "sdk_version": "1.6.0", "kg_snapshot_filename": "kg_snapshot.sqlite",
        "kg_entity_count": len(entities), "kg_snapshot_hash": compute_kg_snapshot_hash(entities, edges),
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    h = compute_entry_hash("", content)

    if sign_hmac:
        sig = base64.b64encode(hmac.new(HMAC_SECRET.encode(), h.encode(), hashlib.sha256).digest()).decode()
        signer_id = "hmac-sha256:fallback"
    else:
        sig = base64.b64encode(ed25519_private_key.sign(h.encode())).decode()
        signer_id = "ed25519:substrate-v1"

    manifest = BundleManifest(**content, content_hash=h, signer_id=signer_id, signature=sig)
    return manifest, entities, edges


def test_bundle_save_and_verify_hmac(tmp_path):
    manifest, entities, edges = _build_manifest(str(uuid4()), str(uuid4()), sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    verified = verify_bundle(str(bundle_dir), hmac_secret=HMAC_SECRET)
    assert verified.bundle_ref == manifest.bundle_ref
    assert verified.kg_entity_count == 2


def test_bundle_verify_rejects_wrong_hmac_secret(tmp_path):
    manifest, entities, edges = _build_manifest(str(uuid4()), str(uuid4()), sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    with pytest.raises(BundleVerificationError):
        verify_bundle(str(bundle_dir), hmac_secret="wrong-secret")


def test_bundle_verify_ed25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    manifest, entities, edges = _build_manifest(str(uuid4()), str(uuid4()), sign_hmac=False, ed25519_private_key=private_key)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp) / "bundle"
        save_bundle(str(bundle_dir), manifest, kg_bytes)
        verified = verify_bundle(str(bundle_dir), public_key_pem=public_pem)
        assert verified.signer_id == "ed25519:substrate-v1"


def test_bundle_verify_detects_tampered_content(tmp_path):
    manifest, entities, edges = _build_manifest(str(uuid4()), str(uuid4()), sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    # Tamper with the manifest on disk after the fact.
    manifest_path = bundle_dir / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["agents"][0]["name"] = "Malicious Agent"
    manifest_path.write_text(json.dumps(data))

    with pytest.raises(BundleVerificationError, match="content hash mismatch"):
        verify_bundle(str(bundle_dir), hmac_secret=HMAC_SECRET)


def test_bundle_verify_detects_tampered_kg_snapshot(tmp_path):
    manifest, entities, edges = _build_manifest(str(uuid4()), str(uuid4()), sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    import sqlite3
    conn = sqlite3.connect(str(bundle_dir / "kg_snapshot.sqlite"))
    conn.execute("UPDATE entities SET entity_key = 'HACKED' WHERE id = 'ent-1'")
    conn.commit()
    conn.close()

    with pytest.raises(BundleVerificationError, match="KG snapshot content hash mismatch"):
        verify_bundle(str(bundle_dir), hmac_secret=HMAC_SECRET)


async def test_runtime_initializes_from_bundle_with_zero_network_calls(tmp_path):
    tenant_id = str(uuid4())
    agent_id = str(uuid4())
    manifest, entities, edges = _build_manifest(tenant_id, agent_id, sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    settings = Settings(
        BUNDLE_PATH=str(bundle_dir), TENANT_ID=tenant_id,
        EXECUTION_STORE_PATH=str(tmp_path / "executions.db"),
    )
    runtime = AgentRuntime(settings)

    # Prove zero network calls: constructing an httpx.AsyncClient during
    # initialize_from_bundle() would mean something tried to reach the
    # network. Fail loudly if that ever happens.
    with patch("httpx.AsyncClient.__init__", side_effect=AssertionError("network client constructed during offline init")):
        result_manifest = await runtime.initialize_from_bundle(str(bundle_dir), hmac_secret=HMAC_SECRET)

    assert result_manifest.bundle_ref == manifest.bundle_ref
    assert runtime.is_ready()
    assert runtime._control_plane_client is None
    assert runtime._offline is True
    assert len(runtime.list_agents()) == 1
    assert runtime._local_kg is not None
    assert await runtime._local_kg.count() == 2

    await runtime.shutdown()


async def test_runtime_offline_agent_has_seeded_semantic_memory(tmp_path):
    tenant_id = str(uuid4())
    agent_id = str(uuid4())
    manifest, entities, edges = _build_manifest(tenant_id, agent_id, sign_hmac=True)
    kg_bytes = _build_kg_snapshot_sqlite(entities, edges)
    bundle_dir = tmp_path / "bundle"
    save_bundle(str(bundle_dir), manifest, kg_bytes)

    settings = Settings(
        BUNDLE_PATH=str(bundle_dir), TENANT_ID=tenant_id,
        EXECUTION_STORE_PATH=str(tmp_path / "executions.db"),
    )
    runtime = AgentRuntime(settings)
    await runtime.initialize_from_bundle(str(bundle_dir), hmac_secret=HMAC_SECRET)

    agent_data = runtime._agents[agent_id]
    agent = agent_data["instance"]
    assert agent.pipeline.semantic.local_only is True
    assert agent.pipeline.semantic._chroma_client is None

    # Seeded from the KG snapshot via upsert_entity() -- search_entities()'s
    # local_only path does substring matching, no vector search / no network.
    results = await agent.pipeline.semantic.search_entities(agent_id, "Postgres primary", top_k=5)
    assert any(e.entity_key == "Postgres primary" for e in results)

    # And it's actually reachable through the normal MemoryContext path a
    # real run() would use, not just directly. The local_only fallback's
    # substring match requires the *query* to appear inside entity_key
    # (not the reverse) -- a real limitation of plain substring matching
    # with no embeddings, pre-existing and out of scope for C4 to fix.
    context = await agent.pipeline.prepare_context("session-1", "Postgres primary")
    assert any(e.entity_key == "Postgres primary" for e in context.semantic_entities)

    await runtime.shutdown()


async def test_initialize_requires_bundle_or_control_plane_url():
    settings = Settings()
    runtime = AgentRuntime(settings)
    with pytest.raises(ValueError, match="BUNDLE_PATH"):
        await runtime.initialize()
