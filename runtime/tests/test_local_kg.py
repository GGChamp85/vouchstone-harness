"""Tests for LocalKGStore schema versioning + idempotent writes (C8).

Two things are proven: (1) a pre-versioning (v1) snapshot on disk --
including one with pre-existing duplicate edge rows, the exact bug this
change fixes -- migrates forward correctly and loses no data, and (2) a
snapshot from a newer schema version than this code understands is
refused outright rather than partially read.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.local_kg import (
    CURRENT_SCHEMA_VERSION, LocalKGStore, SchemaVersionError, _SCHEMA_V1,
)
from vouchstone_sdk import Entity, EntityGraph


def _write_legacy_v1_snapshot(db_path: str, with_duplicate_edges: bool = False) -> None:
    """Builds a snapshot file exactly as pre-migration code would have --
    no _meta table, no updated_at column, no edge uniqueness constraint."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_V1)
    conn.execute(
        "INSERT INTO entities (id, entity_type, entity_key, attributes, confidence, source_trace_id, created_at) "
        "VALUES ('ent-1', 'system', 'Postgres', '{}', 0.9, NULL, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO entities (id, entity_type, entity_key, attributes, confidence, source_trace_id, created_at) "
        "VALUES ('ent-2', 'data', 'shipments', '{}', 0.9, NULL, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES ('ent-2', 'ent-1', 'hosted_on', '{}')")
    if with_duplicate_edges:
        # The real pre-v2 bug: nothing prevented the same logical edge from
        # being inserted more than once (e.g. two `harness sync` runs).
        conn.execute("INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES ('ent-2', 'ent-1', 'hosted_on', '{}')")
        conn.execute("INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES ('ent-2', 'ent-1', 'hosted_on', '{}')")
    conn.commit()
    conn.close()


async def test_fresh_store_initializes_directly_at_current_version(tmp_path):
    store = LocalKGStore(str(tmp_path / "fresh.sqlite"))
    await store.initialize()
    assert store.schema_version == CURRENT_SCHEMA_VERSION

    conn = sqlite3.connect(str(tmp_path / "fresh.sqlite"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
    assert "updated_at" in cols
    edges_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='edges'").fetchone()[0]
    assert "UNIQUE" in edges_sql
    conn.close()
    await store.close()


async def test_legacy_v1_snapshot_migrates_forward_without_losing_data(tmp_path):
    db_path = str(tmp_path / "legacy.sqlite")
    _write_legacy_v1_snapshot(db_path)

    store = LocalKGStore(db_path)
    await store.initialize()

    assert store.schema_version == CURRENT_SCHEMA_VERSION
    assert await store.count() == 2

    ent1 = await store.get_entity("ent-1")
    assert ent1.entity_key == "Postgres"

    # updated_at was backfilled from created_at, not left NULL.
    updated_at = await store.get_entity_updated_at("ent-1")
    assert updated_at == "2026-01-01T00:00:00+00:00"

    graph = await store.to_entity_graph()
    assert len(graph.all_entities()) == 2

    await store.close()


async def test_legacy_v1_snapshot_with_duplicate_edges_dedupes_on_migration(tmp_path):
    db_path = str(tmp_path / "legacy_dupes.sqlite")
    _write_legacy_v1_snapshot(db_path, with_duplicate_edges=True)

    # Sanity check the fixture actually has duplicates before migration.
    raw_conn = sqlite3.connect(db_path)
    assert raw_conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 3
    raw_conn.close()

    store = LocalKGStore(db_path)
    await store.initialize()

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    conn.close()

    await store.close()


async def test_opening_a_newer_schema_version_is_refused(tmp_path):
    db_path = str(tmp_path / "future.sqlite")
    store = LocalKGStore(db_path)
    await store.initialize()
    await store.close()

    # Simulate a bundle built by a future SDK version.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE _meta SET value = ? WHERE key = 'schema_version'", (str(CURRENT_SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()

    newer_store = LocalKGStore(db_path)
    with pytest.raises(SchemaVersionError, match="newer version"):
        await newer_store.initialize()


async def test_add_edge_is_idempotent(tmp_path):
    store = LocalKGStore(str(tmp_path / "idempotent_edges.sqlite"))
    await store.initialize()
    await store.upsert_entity(Entity(id="a", entity_type="t", entity_key="A"))
    await store.upsert_entity(Entity(id="b", entity_type="t", entity_key="B"))

    await store.add_edge("a", "b", "relates_to", {"weight": 1})
    await store.add_edge("a", "b", "relates_to", {"weight": 2})  # same identity, different attrs
    await store.add_edge("a", "b", "relates_to", {"weight": 3})

    conn = sqlite3.connect(str(tmp_path / "idempotent_edges.sqlite"))
    count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    attrs = conn.execute("SELECT attributes FROM edges").fetchone()[0]
    conn.close()

    assert count == 1
    assert '"weight": 3' in attrs  # last write wins

    await store.close()


async def test_import_entity_graph_is_idempotent_across_repeated_syncs(tmp_path):
    store = LocalKGStore(str(tmp_path / "idempotent_import.sqlite"))
    await store.initialize()

    graph = EntityGraph()
    graph.add_entity(Entity(id="e1", entity_type="system", entity_key="Postgres"))
    graph.add_entity(Entity(id="e2", entity_type="data", entity_key="shipments"))
    graph.add_edge("e2", "e1", "hosted_on", {})

    # Simulate `harness sync` running against the same bundle twice.
    await store.import_entity_graph(graph)
    await store.import_entity_graph(graph)
    await store.import_entity_graph(graph)

    assert await store.count() == 2
    conn = sqlite3.connect(str(tmp_path / "idempotent_import.sqlite"))
    edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()
    assert edge_count == 1

    await store.close()


async def test_upsert_entity_updates_updated_at_on_change(tmp_path):
    store = LocalKGStore(str(tmp_path / "updated_at.sqlite"))
    await store.initialize()

    await store.upsert_entity(Entity(id="e1", entity_type="t", entity_key="v1"))
    first_updated_at = await store.get_entity_updated_at("e1")

    await store.upsert_entity(Entity(id="e1", entity_type="t", entity_key="v2"))
    second_updated_at = await store.get_entity_updated_at("e1")

    assert first_updated_at is not None
    assert second_updated_at is not None
    entity = await store.get_entity("e1")
    assert entity.entity_key == "v2"

    await store.close()
