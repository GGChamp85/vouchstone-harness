"""Embedded, persistent local KG snapshot store (C4 — offline harness mode).

Backs the offline bundle's knowledge-graph snapshot with a real SQLite file
instead of ephemeral in-memory state, so an offline harness process can be
restarted without losing what it pulled. Bridges directly to the SDK's
``EntityGraph`` (C6) — the snapshot IS an EntityGraph, just persisted.

Stdlib ``sqlite3`` only — no new dependency, matches the C4 acceptance
criteria ("embedded SQLite/DuckDB"). sqlite3 is synchronous; methods here
run it via ``asyncio.to_thread`` to keep the runtime's async call sites
uniform without blocking the event loop on disk I/O.

Schema versioning (C8): new node/edge attributes get added over time (this
file's own history added ``entities.updated_at`` and an edge uniqueness
constraint in v2) and a customer's already-pulled bundle on disk must not
silently break or lose data when it's opened by newer runtime code.
``_meta`` records the schema version a given snapshot file was written
with; ``initialize()`` migrates it forward in-place through ``MIGRATIONS``
before any query runs. A snapshot stamped with a version newer than this
code understands is refused outright (``SchemaVersionError``) rather than
read partially/incorrectly.

Incremental/idempotent sync (C8): v1's ``edges`` table had no uniqueness
constraint, so re-importing the same KG snapshot twice (e.g. re-running
``harness sync`` against a bundle whose KG grew) silently duplicated every
edge that already existed -- a real correctness bug, not a hypothetical
one. v2 adds ``UNIQUE(source_id, target_id, edge_type)`` and both
``add_edge()``/``import_entity_graph()`` now upsert instead of blindly
inserting, so repeated syncs converge instead of accumulating duplicates.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from vouchstone_sdk import Edge, Entity, EntityGraph

CURRENT_SCHEMA_VERSION = 2

# v1 shape (kept only as migration history/documentation -- new stores are
# created directly at CURRENT_SCHEMA_VERSION via _SCHEMA below, never at v1).
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    source_trace_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES entities(id),
    FOREIGN KEY (target_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS ix_edges_source ON edges(source_id, edge_type);
"""

# v2: adds entities.updated_at (upsert_entity() sets it on every write, so
# callers can distinguish "first extracted" from "last changed") and an
# edges uniqueness constraint (makes edge writes idempotent -- see module
# docstring).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    source_trace_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES entities(id),
    FOREIGN KEY (target_id) REFERENCES entities(id),
    UNIQUE(source_id, target_id, edge_type)
);
CREATE INDEX IF NOT EXISTS ix_edges_source ON edges(source_id, edge_type);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SchemaVersionError(Exception):
    """Raised when a snapshot's stored schema version is newer than this
    code understands (an older runtime opening a bundle built by newer
    code), or a migration step itself fails. Never silently read a
    snapshot whose shape isn't fully understood."""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
    if "updated_at" not in existing_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE entities SET updated_at = created_at WHERE updated_at IS NULL")

    # SQLite can't add a UNIQUE constraint to an existing table via ALTER
    # TABLE -- rebuild it. Dedup by keeping the last-written row per
    # (source_id, target_id, edge_type): rowid ascending = insertion order,
    # so "last" is genuinely the most recently written attributes, not an
    # arbitrary pick.
    already_migrated = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    if already_migrated and "UNIQUE" in (already_migrated[0] or ""):
        return

    conn.execute("""
        CREATE TABLE edges_v2 (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            attributes TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (source_id) REFERENCES entities(id),
            FOREIGN KEY (target_id) REFERENCES entities(id),
            UNIQUE(source_id, target_id, edge_type)
        )
    """)
    for row in conn.execute("SELECT source_id, target_id, edge_type, attributes FROM edges ORDER BY rowid ASC"):
        conn.execute(
            """INSERT INTO edges_v2 (source_id, target_id, edge_type, attributes) VALUES (?, ?, ?, ?)
               ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET attributes=excluded.attributes""",
            row,
        )
    conn.execute("DROP TABLE edges")
    conn.execute("ALTER TABLE edges_v2 RENAME TO edges")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_edges_source ON edges(source_id, edge_type)")


# Keyed by the version a migration produces. Applied in ascending order for
# every version strictly between a snapshot's stored version and
# CURRENT_SCHEMA_VERSION.
MIGRATIONS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
}


class LocalKGStore:
    """A single-file, embedded KG snapshot. One store per bundle."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self.schema_version: int = CURRENT_SCHEMA_VERSION

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_META_SCHEMA)

        entities_table_exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entities'"
        ).fetchone() is not None

        if not entities_table_exists:
            # Brand-new snapshot file -- create directly at the current
            # schema, no migration needed.
            self._conn.executescript(_SCHEMA)
            self._set_schema_version(CURRENT_SCHEMA_VERSION)
            self._conn.commit()
            self.schema_version = CURRENT_SCHEMA_VERSION
            return

        stored_version = self._get_schema_version()
        if stored_version is None:
            # Pre-versioning snapshot on disk (entities table exists but no
            # _meta row) -- these were always written at v1.
            stored_version = 1

        if stored_version > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"snapshot at {self.db_path} is schema v{stored_version}, but this runtime "
                f"only understands up to v{CURRENT_SCHEMA_VERSION} -- refusing to open a "
                f"snapshot from a newer version of the SDK. Upgrade the runtime/SDK first."
            )

        for version in range(stored_version + 1, CURRENT_SCHEMA_VERSION + 1):
            migrate = MIGRATIONS.get(version)
            if migrate is None:
                raise SchemaVersionError(f"no migration registered to reach schema v{version}")
            try:
                migrate(self._conn)
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise SchemaVersionError(f"migration to schema v{version} failed: {exc}") from exc
            self._set_schema_version(version)
            self._conn.commit()

        self.schema_version = CURRENT_SCHEMA_VERSION

    def _get_schema_version(self) -> Optional[int]:
        row = self._conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else None

    def _set_schema_version(self, version: int) -> None:
        self._conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("LocalKGStore not initialized -- call initialize() first")
        return self._conn

    async def upsert_entity(self, entity: Entity) -> None:
        await asyncio.to_thread(self._upsert_entity_sync, entity)

    def _upsert_entity_sync(self, entity: Entity) -> None:
        # updated_at (schema v2) isn't a field on the SDK's Entity dataclass
        # -- it's a store-level write-time stamp, not part of the entity's
        # own semantic content, so it's set here rather than threaded
        # through Entity itself. Same value on first insert (creation IS
        # the first update) and on every subsequent upsert.
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._require_conn()
        conn.execute(
            """INSERT INTO entities (id, entity_type, entity_key, attributes, confidence, source_trace_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 entity_type=excluded.entity_type, entity_key=excluded.entity_key,
                 attributes=excluded.attributes, confidence=excluded.confidence,
                 source_trace_id=excluded.source_trace_id, updated_at=excluded.updated_at""",
            (
                entity.id, entity.entity_type, entity.entity_key,
                json.dumps(entity.attributes), entity.confidence, entity.source_trace_id,
                entity.created_at.isoformat(), now_iso,
            ),
        )
        conn.commit()

    async def get_entity_updated_at(self, entity_id: str) -> Optional[str]:
        """Store-level write-time stamp (schema v2) -- not part of Entity
        itself, exposed separately for callers that need it (e.g. sync
        diffing, audit views)."""
        return await asyncio.to_thread(self._get_entity_updated_at_sync, entity_id)

    def _get_entity_updated_at_sync(self, entity_id: str) -> Optional[str]:
        conn = self._require_conn()
        row = conn.execute("SELECT updated_at FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return row[0] if row else None

    async def add_edge(self, source_id: str, target_id: str, edge_type: str, attributes: Optional[dict] = None) -> None:
        await asyncio.to_thread(self._add_edge_sync, source_id, target_id, edge_type, attributes or {})

    def _add_edge_sync(self, source_id: str, target_id: str, edge_type: str, attributes: dict) -> None:
        conn = self._require_conn()
        conn.execute(
            """INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES (?, ?, ?, ?)
               ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET attributes=excluded.attributes""",
            (source_id, target_id, edge_type, json.dumps(attributes)),
        )
        conn.commit()

    async def get_entity(self, entity_id: str) -> Optional[Entity]:
        return await asyncio.to_thread(self._get_entity_sync, entity_id)

    def _get_entity_sync(self, entity_id: str) -> Optional[Entity]:
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._row_to_entity(row) if row else None

    async def entities_by_type(self, entity_type: str) -> List[Entity]:
        return await asyncio.to_thread(self._entities_by_type_sync, entity_type)

    def _entities_by_type_sync(self, entity_type: str) -> List[Entity]:
        conn = self._require_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM entities WHERE entity_type = ?", (entity_type,)).fetchall()
        conn.row_factory = None
        return [self._row_to_entity(r) for r in rows]

    def _row_to_entity(self, row) -> Entity:
        if not isinstance(row, sqlite3.Row):
            conn = self._require_conn()
            cols = [d[0] for d in conn.execute("SELECT * FROM entities LIMIT 0").description]
            row = dict(zip(cols, row))
        return Entity(
            id=row["id"], entity_type=row["entity_type"], entity_key=row["entity_key"],
            attributes=json.loads(row["attributes"]), confidence=row["confidence"],
            source_trace_id=row["source_trace_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def to_entity_graph(self) -> EntityGraph:
        """Load the full snapshot into an in-memory EntityGraph (C6) for a
        single agent run -- the SQLite file remains the durable source of
        truth; this is a working-set view over it."""
        return await asyncio.to_thread(self._to_entity_graph_sync)

    def _to_entity_graph_sync(self) -> EntityGraph:
        conn = self._require_conn()
        conn.row_factory = sqlite3.Row
        graph = EntityGraph()
        for row in conn.execute("SELECT * FROM entities").fetchall():
            graph.add_entity(self._row_to_entity(row))
        for row in conn.execute("SELECT * FROM edges").fetchall():
            graph.add_edge(row["source_id"], row["target_id"], row["edge_type"], json.loads(row["attributes"]))
        conn.row_factory = None
        return graph

    async def import_entity_graph(self, graph: EntityGraph) -> None:
        """Persist an EntityGraph's full contents into this store (used when
        materializing a pulled bundle's KG snapshot for the first time)."""
        data = graph.to_dict()
        await asyncio.to_thread(self._import_entity_graph_sync, data)

    def _import_entity_graph_sync(self, data: dict) -> None:
        # Idempotent by design (C8): importing the same graph twice (e.g.
        # `harness sync` re-pulling a bundle) must converge, not
        # accumulate duplicate rows -- both entities and edges upsert on
        # their natural identity (entity id; (source, target, edge_type)
        # for edges, matching the v2 UNIQUE constraint).
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._require_conn()
        for e in data.get("entities", []):
            conn.execute(
                """INSERT INTO entities (id, entity_type, entity_key, attributes, confidence, source_trace_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     entity_type=excluded.entity_type, entity_key=excluded.entity_key,
                     attributes=excluded.attributes, confidence=excluded.confidence,
                     updated_at=excluded.updated_at""",
                (
                    e["id"], e["entity_type"], e["entity_key"], json.dumps(e.get("attributes", {})),
                    e.get("confidence", 1.0), e.get("source_trace_id"),
                    e.get("created_at") or now_iso, now_iso,
                ),
            )
        for ed in data.get("edges", []):
            conn.execute(
                """INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET attributes=excluded.attributes""",
                (ed["source_id"], ed["target_id"], ed["edge_type"], json.dumps(ed.get("attributes", {}))),
            )
        conn.commit()

    async def count(self) -> int:
        return await asyncio.to_thread(lambda: self._require_conn().execute("SELECT COUNT(*) FROM entities").fetchone()[0])

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
