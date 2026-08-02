"""Durable execution tracking for long-running agent runs.

Addresses a real gap: AgentRuntime.execute() was a single in-memory
request/response call with nothing persisted -- a long-running agent task
had no record that survived a process restart, and no way to report
progress partway through. This store makes execution state durable
(embedded SQLite, same pattern as local_kg.py) and adds a checkpoint
mechanism agents can use to report progress during a long run.

This does NOT solve horizontal scaling (multiple runtime replicas sharing
load via a distributed queue) -- that's a separate, larger infrastructure
decision (queue technology, worker coordination, deployment topology) and
is tracked separately. What this solves: within a single runtime process,
an execution's status/checkpoints survive a restart, and a crash mid-run
is detected (not silently lost) the next time the process starts.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL,
    input_data TEXT NOT NULL DEFAULT '{}',
    output_data TEXT,
    checkpoint_data TEXT,
    checkpoint_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_executions_agent ON executions(agent_id, status);
CREATE INDEX IF NOT EXISTS ix_executions_status ON executions(status);
"""

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# Set on startup for any execution found STATUS_RUNNING from a prior
# process -- the process died mid-run; this is never set by execute()
# itself. Distinct from FAILED (an execution that ran to completion and
# raised) so operators can tell "crashed" from "errored" at a glance.
STATUS_INTERRUPTED = "interrupted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionStore:
    """One store per runtime process. Embedded SQLite -- no external
    dependency, durable across restarts of this process on the same disk."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ExecutionStore not initialized -- call initialize() first")
        return self._conn

    async def start_execution(
        self, execution_id: str, agent_id: str, input_data: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(self._start_execution_sync, execution_id, agent_id, input_data, session_id)

    def _start_execution_sync(self, execution_id, agent_id, input_data, session_id) -> None:
        conn = self._require_conn()
        now = _now()
        conn.execute(
            """INSERT INTO executions (id, agent_id, session_id, status, input_data, started_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (execution_id, agent_id, session_id, STATUS_RUNNING, json.dumps(input_data), now, now),
        )
        conn.commit()

    async def checkpoint(self, execution_id: str, checkpoint_data: Dict[str, Any]) -> None:
        """Record progress partway through a long-running execution. Safe to
        call multiple times -- each call overwrites the latest checkpoint
        and bumps checkpoint_count, so callers can tell how many checkpoints
        have fired without keeping full history (that's what a real
        WorkflowTrace, C6, is for if full history matters)."""
        await asyncio.to_thread(self._checkpoint_sync, execution_id, checkpoint_data)

    def _checkpoint_sync(self, execution_id: str, checkpoint_data: Dict[str, Any]) -> None:
        conn = self._require_conn()
        conn.execute(
            """UPDATE executions SET checkpoint_data = ?, checkpoint_count = checkpoint_count + 1, updated_at = ?
               WHERE id = ? AND status = ?""",
            (json.dumps(checkpoint_data), _now(), execution_id, STATUS_RUNNING),
        )
        conn.commit()

    async def complete_execution(self, execution_id: str, output_data: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._finish_sync, execution_id, STATUS_COMPLETED, output_data=output_data)

    async def fail_execution(self, execution_id: str, error: str) -> None:
        await asyncio.to_thread(self._finish_sync, execution_id, STATUS_FAILED, error=error)

    def _finish_sync(
        self, execution_id: str, status: str,
        output_data: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
    ) -> None:
        conn = self._require_conn()
        now = _now()
        conn.execute(
            """UPDATE executions SET status = ?, output_data = ?, error = ?, updated_at = ?, completed_at = ?
               WHERE id = ?""",
            (status, json.dumps(output_data) if output_data is not None else None, error, now, now, execution_id),
        )
        conn.commit()

    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_execution_sync, execution_id)

    def _get_execution_sync(self, execution_id: str) -> Optional[Dict[str, Any]]:
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    async def list_executions(
        self, agent_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_executions_sync, agent_id, status, limit)

    def _list_executions_sync(self, agent_id, status, limit) -> List[Dict[str, Any]]:
        conn = self._require_conn()
        query = "SELECT * FROM executions WHERE 1=1"
        params: List[Any] = []
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def mark_interrupted_on_startup(self) -> List[Dict[str, Any]]:
        """Call once at runtime startup, before accepting new work. Any
        execution still STATUS_RUNNING from a prior process instance did
        not finish normally -- the process was killed, crashed, or was
        redeployed mid-execution. Marks them INTERRUPTED (not silently
        left as "running" forever, which would be actively misleading) and
        returns them so the caller can log/alert/retry."""
        return await asyncio.to_thread(self._mark_interrupted_sync)

    def _mark_interrupted_sync(self) -> List[Dict[str, Any]]:
        conn = self._require_conn()
        rows = conn.execute("SELECT * FROM executions WHERE status = ?", (STATUS_RUNNING,)).fetchall()
        interrupted = [self._row_to_dict(r) for r in rows]
        if interrupted:
            conn.execute(
                "UPDATE executions SET status = ?, updated_at = ? WHERE status = ?",
                (STATUS_INTERRUPTED, _now(), STATUS_RUNNING),
            )
            conn.commit()
        return interrupted

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key in ("input_data", "output_data", "checkpoint_data"):
            if d.get(key):
                d[key] = json.loads(d[key])
        return d

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
