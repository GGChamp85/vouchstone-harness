"""Durable execution tracking tests.

Proves the actual claims: execution state survives a new ExecutionStore
instance pointed at the same file (simulating a process restart), a
checkpoint reported mid-run is durably recorded, and an execution left
"running" when the process stops gets detected and marked interrupted on
the next startup -- not silently left looking healthy forever.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.execution_store import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_RUNNING,
    ExecutionStore,
)
from src.runtime import AgentRuntime


async def test_execution_lifecycle(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions.db"))
    await store.initialize()

    execution_id = str(uuid4())
    await store.start_execution(execution_id, "agent-1", {"task": "migrate shipments table"})

    record = await store.get_execution(execution_id)
    assert record["status"] == STATUS_RUNNING
    assert record["agent_id"] == "agent-1"
    assert record["input_data"] == {"task": "migrate shipments table"}

    await store.checkpoint(execution_id, {"step": "2/5", "rows_done": 10000})
    record = await store.get_execution(execution_id)
    assert record["checkpoint_data"] == {"step": "2/5", "rows_done": 10000}
    assert record["checkpoint_count"] == 1

    await store.checkpoint(execution_id, {"step": "4/5", "rows_done": 40000})
    record = await store.get_execution(execution_id)
    assert record["checkpoint_data"] == {"step": "4/5", "rows_done": 40000}
    assert record["checkpoint_count"] == 2

    await store.complete_execution(execution_id, {"rows_migrated": 50000})
    record = await store.get_execution(execution_id)
    assert record["status"] == STATUS_COMPLETED
    assert record["output_data"] == {"rows_migrated": 50000}
    assert record["completed_at"] is not None

    await store.close()


async def test_execution_survives_process_restart(tmp_path):
    db_path = str(tmp_path / "executions.db")
    execution_id = str(uuid4())

    store1 = ExecutionStore(db_path)
    await store1.initialize()
    await store1.start_execution(execution_id, "agent-1", {"task": "long job"})
    await store1.checkpoint(execution_id, {"step": "1/10"})
    await store1.close()  # simulates the process stopping

    # A brand new ExecutionStore instance, same file -- this is what
    # happens on a real restart/redeploy.
    store2 = ExecutionStore(db_path)
    await store2.initialize()
    record = await store2.get_execution(execution_id)
    assert record is not None
    assert record["checkpoint_data"] == {"step": "1/10"}
    await store2.close()


async def test_interrupted_execution_detected_on_restart(tmp_path):
    db_path = str(tmp_path / "executions.db")
    execution_id = str(uuid4())

    store1 = ExecutionStore(db_path)
    await store1.initialize()
    await store1.start_execution(execution_id, "agent-1", {"task": "job that crashes"})
    # No complete/fail call -- simulates the process dying mid-execution.
    await store1.close()

    store2 = ExecutionStore(db_path)
    await store2.initialize()
    interrupted = await store2.mark_interrupted_on_startup()
    assert len(interrupted) == 1
    assert interrupted[0]["id"] == execution_id

    record = await store2.get_execution(execution_id)
    assert record["status"] == STATUS_INTERRUPTED
    await store2.close()


async def test_mark_interrupted_does_not_touch_completed_or_failed(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions.db"))
    await store.initialize()

    completed_id, failed_id, running_id = str(uuid4()), str(uuid4()), str(uuid4())
    await store.start_execution(completed_id, "agent-1", {})
    await store.complete_execution(completed_id, {"ok": True})
    await store.start_execution(failed_id, "agent-1", {})
    await store.fail_execution(failed_id, "boom")
    await store.start_execution(running_id, "agent-1", {})

    interrupted = await store.mark_interrupted_on_startup()
    assert [e["id"] for e in interrupted] == [running_id]

    assert (await store.get_execution(completed_id))["status"] == STATUS_COMPLETED
    assert (await store.get_execution(failed_id))["status"] == STATUS_FAILED
    assert (await store.get_execution(running_id))["status"] == STATUS_INTERRUPTED
    await store.close()


async def test_list_executions_filters(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions.db"))
    await store.initialize()

    for i in range(3):
        eid = str(uuid4())
        await store.start_execution(eid, "agent-a", {"i": i})
        if i == 0:
            await store.complete_execution(eid, {})
    other_id = str(uuid4())
    await store.start_execution(other_id, "agent-b", {})

    agent_a = await store.list_executions(agent_id="agent-a")
    assert len(agent_a) == 3
    assert all(e["agent_id"] == "agent-a" for e in agent_a)

    running_only = await store.list_executions(status=STATUS_RUNNING)
    assert len(running_only) == 3  # 2 from agent-a + 1 from agent-b

    completed_only = await store.list_executions(status=STATUS_COMPLETED)
    assert len(completed_only) == 1
    await store.close()


# ============================================================
# Runtime integration -- execute() wired to the execution store, and a
# real agent reporting checkpoints via self.checkpoint() during run().
# ============================================================

async def test_runtime_execute_tracks_execution_and_checkpoints(tmp_path):
    from vouchstone_sdk import Agent, AgentConfig
    from vouchstone_sdk.types import AgentResponse, Message, MemoryContext

    class CheckpointingAgent(Agent):
        async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
            await self.checkpoint({"step": "1/2", "status": "halfway"})
            await self.checkpoint({"step": "2/2", "status": "done"})
            return AgentResponse(content=f"processed: {message.content}")

    settings = Settings(
        CONTROL_PLANE_URL="https://unused.example.com",
        EXECUTION_STORE_PATH=str(tmp_path / "executions.db"),
    )
    runtime = AgentRuntime(settings)
    await runtime._init_execution_store()

    agent = CheckpointingAgent(AgentConfig(name="Checkpointer", semantic_memory=False, episodic_memory=False, procedural_memory=False))
    await agent.initialize(agent_id="agent-cp", local_only=True)
    runtime._agents["agent-cp"] = {"definition": {"id": "agent-cp", "name": "Checkpointer"}, "instance": agent}

    result = await runtime.execute("agent-cp", {"content": "do the thing"})

    assert result["output"] == "processed: do the thing"
    assert result["execution_id"]

    record = await runtime.get_execution_status(result["execution_id"])
    assert record["status"] == STATUS_COMPLETED
    assert record["agent_id"] == "agent-cp"
    # Last checkpoint reported during run() is what's durably recorded.
    assert record["checkpoint_data"] == {"step": "2/2", "status": "done"}
    assert record["checkpoint_count"] == 2

    # checkpoint_sink is unset again after the call -- doesn't leak into
    # some future unrelated process() call on the same agent instance.
    assert agent._checkpoint_sink is None

    await runtime.shutdown()


async def test_runtime_execute_records_failure(tmp_path):
    from vouchstone_sdk import Agent, AgentConfig
    from vouchstone_sdk.types import AgentResponse, Message, MemoryContext

    class FailingAgent(Agent):
        async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
            raise RuntimeError("simulated failure")

    settings = Settings(
        CONTROL_PLANE_URL="https://unused.example.com",
        EXECUTION_STORE_PATH=str(tmp_path / "executions.db"),
    )
    runtime = AgentRuntime(settings)
    await runtime._init_execution_store()

    agent = FailingAgent(AgentConfig(name="Failer", semantic_memory=False, episodic_memory=False, procedural_memory=False))
    await agent.initialize(agent_id="agent-fail", local_only=True)
    runtime._agents["agent-fail"] = {"definition": {"id": "agent-fail", "name": "Failer"}, "instance": agent}

    with pytest.raises(RuntimeError, match="simulated failure"):
        await runtime.execute("agent-fail", {"content": "trigger failure"})

    executions = await runtime.list_executions(agent_id="agent-fail")
    assert len(executions) == 1
    assert executions[0]["status"] == STATUS_FAILED
    assert executions[0]["error"] == "simulated failure"

    await runtime.shutdown()
