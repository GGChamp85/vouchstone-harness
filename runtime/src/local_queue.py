"""Tiny SQLite-backed offline queue for buffering ledger entries when the
control plane is unreachable. Targets a 48-hour buffer per Tier 2 SLO.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


DEFAULT_PATH = "/var/vouchstone/queue.db"


class LocalQueue:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get(
            "VOUCHSTONE_LOCAL_QUEUE_PATH", DEFAULT_PATH,
        )
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enqueued_at INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    forwarded_at INTEGER
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS ix_pending_entries_forwarded "
                "ON pending_entries(forwarded_at, id)"
            )

    def enqueue(self, entry: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pending_entries (enqueued_at, payload) VALUES (?, ?)",
                (int(time.time()), json.dumps(entry)),
            )
            return int(cur.lastrowid or 0)

    def peek(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "SELECT id, payload FROM pending_entries "
                "WHERE forwarded_at IS NULL ORDER BY id ASC LIMIT ?",
                (int(limit),),
            )
            out: List[Dict[str, Any]] = []
            for row_id, payload in cur.fetchall():
                try:
                    data = json.loads(payload)
                except Exception:
                    data = {}
                out.append({"_queue_id": row_id, **data})
            return out

    def mark_forwarded(self, ids: List[int]) -> int:
        if not ids:
            return 0
        with self._lock, self._conn() as c:
            now = int(time.time())
            qs = ",".join("?" * len(ids))
            cur = c.execute(
                f"UPDATE pending_entries SET forwarded_at = ? WHERE id IN ({qs})",
                [now, *ids],
            )
            return cur.rowcount or 0

    def depth(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "SELECT COUNT(*) FROM pending_entries WHERE forwarded_at IS NULL"
            )
            return int(cur.fetchone()[0])

    def prune_old(self, ttl_seconds: int = 48 * 3600) -> int:
        """Delete forwarded entries older than ttl_seconds."""
        with self._lock, self._conn() as c:
            cutoff = int(time.time()) - int(ttl_seconds)
            cur = c.execute(
                "DELETE FROM pending_entries WHERE forwarded_at IS NOT NULL "
                "AND forwarded_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
