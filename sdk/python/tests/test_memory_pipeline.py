"""MemoryPipeline + individual layers -- the previously-untested half of
memory.py. Covers the local-mode (no backends) flow end-to-end, the
AgentConfig layer toggles (which were documented-but-inert before
MemoryPipeline.enabled_layers existed), the A3 fixes (episodic search via
api_client, MetaMemory's explicit offline status, procedural graph
read-through), and the backend-unavailable contract.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from vouchstone_sdk.memory import (
    EpisodicMemory,
    MemoryBackendUnavailableError,
    MemoryPipeline,
    MetaMemory,
    ProceduralMemory,
    WorkingMemory,
)
from vouchstone_sdk.types import EpisodicTrace, Skill


class FakeApiClient:
    """Duck-typed api_client capturing _get/_post calls -- the same shape
    VouchstoneClient._get/_post now provide for real."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.calls: list[tuple[str, str, Any]] = []
        self._responses = responses or {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        return self._responses.get(path, {})

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        return self._responses.get(path, {})


# ── Local-mode pipeline flow ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_pipeline_prepare_and_process_turn_round_trip():
    pipeline = MemoryPipeline(agent_id="agent-1", local_only=True)
    await pipeline.initialize()

    ctx = await pipeline.prepare_context("sess-1", "hello world")
    assert ctx.working_memory == []  # first turn: nothing before it

    result = await pipeline.process_turn(
        "sess-1", 1, "hello world", "hi there", tokens_in=3, tokens_out=2,
    )
    assert result.episodic_trace_id

    ctx2 = await pipeline.prepare_context("sess-1", "second turn")
    # user turn 1 + assistant turn 1 are now in working memory
    assert [e["role"] for e in ctx2.working_memory] == ["user", "assistant"]
    assert len(ctx2.episodic_context) == 1
    await pipeline.close()


@pytest.mark.asyncio
async def test_disabled_layers_are_skipped_entirely():
    pipeline = MemoryPipeline(
        agent_id="agent-1",
        local_only=True,
        enabled_layers={"working": False, "episodic": False, "semantic": False,
                        "procedural": False, "meta": False},
    )
    await pipeline.initialize()

    ctx = await pipeline.prepare_context("sess-1", "anything")
    assert ctx.working_memory == []
    assert ctx.episodic_context == []
    assert ctx.semantic_entities == []
    assert ctx.procedural_skills == []

    result = await pipeline.process_turn("sess-1", 1, "anything", "reply")
    assert result.episodic_trace_id == ""  # episodic disabled: no trace
    assert pipeline.episodic._local_traces == []
    # working disabled: the turn also never touched working memory
    assert pipeline.working._local == {}

    reflection = await pipeline.run_reflection("sess-1")
    assert reflection["status"] == "disabled"
    maintenance = await pipeline.run_maintenance()
    assert maintenance["status"] == "disabled"
    await pipeline.close()


@pytest.mark.asyncio
async def test_config_threading_embedding_model_and_retention():
    pipeline = MemoryPipeline(
        agent_id="agent-1", local_only=True,
        embedding_model="text-embedding-3-large", retention_days=30,
    )
    assert pipeline.semantic.embedding_model == "text-embedding-3-large"
    assert pipeline.episodic.retention_days == 30


# ── Backend-unavailable contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_working_memory_unreachable_redis_raises_not_falls_back():
    # Port 9 (discard) on localhost: connection refused immediately.
    wm = WorkingMemory(redis_url="redis://127.0.0.1:9/0")
    with pytest.raises(MemoryBackendUnavailableError):
        await wm.initialize()


# ── A3: episodic search honors api_client ────────────────────────────


@pytest.mark.asyncio
async def test_episodic_search_local_mode():
    ep = EpisodicMemory()
    await ep.append_trace("agent-1", EpisodicTrace(
        id="t1", session_id="s", turn_number=1,
        user_input="migrate the postgres database", agent_response="done",
    ))
    await ep.append_trace("agent-1", EpisodicTrace(
        id="t2", session_id="s", turn_number=2,
        user_input="unrelated question", agent_response="answer",
    ))
    hits = await ep.search("postgres")
    assert [t.id for t in hits] == ["t1"]


@pytest.mark.asyncio
async def test_episodic_search_uses_api_client_when_configured():
    api = FakeApiClient(responses={
        "/memory-pipeline/snapshot/agent-1": {
            "episodic": [
                {"id": "t1", "user_input": "postgres migration", "agent_response": "ok"},
                {"id": "t2", "user_input": "other", "agent_response": "ok"},
            ]
        }
    })
    ep = EpisodicMemory(api_client=api)
    hits = await ep.search("postgres", agent_id="agent-1")
    assert [t["id"] for t in hits] == ["t1"]
    assert api.calls and api.calls[0][1] == "/memory-pipeline/snapshot/agent-1"


@pytest.mark.asyncio
async def test_episodic_search_with_api_client_requires_agent_id():
    ep = EpisodicMemory(api_client=FakeApiClient())
    with pytest.raises(ValueError):
        await ep.search("query")


# ── A3: MetaMemory offline status is explicit ────────────────────────


@pytest.mark.asyncio
async def test_meta_memory_offline_returns_unavailable_not_success_shapes():
    meta = MetaMemory()
    maintenance = await meta.run_maintenance("agent-1")
    assert maintenance["status"] == "unavailable"
    reflection = await meta.run_reflection("agent-1", "sess-1")
    assert reflection["status"] == "unavailable"
    health = await meta.get_health("agent-1")
    assert any("unavailable" in r for r in health.recommendations)


# ── A3: procedural skills survive via graph read-through ─────────────


class FakeNeo4jResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class FakeNeo4jSession:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self.queries: list[str] = []

    async def __aenter__(self) -> FakeNeo4jSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def run(self, query: str, **kwargs: Any) -> FakeNeo4jResult:
        self.queries.append(query)
        if query.strip().startswith("MATCH"):
            return FakeNeo4jResult(self._rows)
        return FakeNeo4jResult([])


class FakeNeo4jDriver:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def session(self) -> FakeNeo4jSession:
        return FakeNeo4jSession(self._rows)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_list_skills_reads_back_from_graph_backend():
    """Skills persisted to the graph in a PREVIOUS process must be visible
    to find/list in this one -- previously reads only consulted the
    in-process dict, so every skill vanished on restart."""
    pm = ProceduralMemory(graph_db_url="bolt://example:7687")
    pm._driver = FakeNeo4jDriver(rows=[{
        "s": {
            "name": "reconcile-invoices",
            "description": "Match invoices to POs",
            "steps": json.dumps(["fetch", "match", "flag"]),
            "tools_required": json.dumps(["erp_api"]),
            "version": 3,
            "success_rate": 0.9,
            "execution_count": 12,
        }
    }])

    skills = await pm.list_skills("agent-1")
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "reconcile-invoices"
    assert s.steps == ["fetch", "match", "flag"]
    assert s.version == 3

    found = await pm.find_skill("agent-1", "invoices")
    assert [x.name for x in found] == ["reconcile-invoices"]


@pytest.mark.asyncio
async def test_local_skill_wins_over_graph_copy_on_name_collision():
    pm = ProceduralMemory(graph_db_url="bolt://example:7687")
    pm._driver = FakeNeo4jDriver(rows=[{
        "s": {"name": "reconcile-invoices", "description": "stale graph copy",
              "steps": "[]", "tools_required": "[]", "version": 1,
              "success_rate": 0.0, "execution_count": 0}
    }])
    fresh = Skill(id="x", name="reconcile-invoices",
                  description="fresh in-session copy", execution_count=5)
    pm._local_skills["agent-1:reconcile-invoices"] = fresh

    skills = await pm.list_skills("agent-1")
    assert len(skills) == 1
    assert skills[0].description == "fresh in-session copy"
    assert skills[0].execution_count == 5
