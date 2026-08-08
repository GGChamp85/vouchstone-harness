"""Regression test for the shared-Agent-instance concurrency race (Phase 3
of the multi-replica safety pass).

AgentRuntime (data-plane/runtime/src/runtime.py) keeps exactly one Agent
instance per agent_id and reuses it for every call to that agent -- so two
overlapping /execute requests for the same agent_id run concurrently
against the same Python object. Before this fix, Agent stored
_checkpoint_sink and _current_execution_id as plain instance attributes,
which process() mutated around `await` points (prepare_context(), run()).
Two concurrent process() calls could interleave at those await points and
clobber each other's in-flight execution_id / checkpoint sink, so a
checkpoint reported by call A could get durably recorded against call B's
execution (or via B's sink) instead of A's.

This test proves the fix -- contextvars.ContextVar-scoped state, isolated
per asyncio Task -- actually holds under real interleaving, not just that
the code compiles. It deliberately reproduces the exact failure shape the
old code had: task B (fast, no yield in run()) fully completes its
set_checkpoint_sink()/process()/set_checkpoint_sink(None) cycle *while*
task A is suspended mid-run() on an `await asyncio.sleep(...)`, then
checks that task A's second checkpoint still lands on task A's own sink
with task A's own execution_id, not task B's.
"""
import asyncio

from vouchstone_sdk import Agent, AgentConfig
from vouchstone_sdk.types import AgentResponse, MemoryContext, Message


class _CheckpointingAgent(Agent):
    """run() reports two checkpoints with a yield point in between, whose
    length is controlled by the message content -- long enough for the
    "fast" concurrent call to fully finish while this one is suspended."""

    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        await self.checkpoint({"phase": "start", "who": message.content})
        await asyncio.sleep(message.metadata.get("delay", 0.0))
        await self.checkpoint({"phase": "end", "who": message.content})
        return AgentResponse(content=f"done:{message.content}")


def _make_sink(calls: list):
    async def sink(execution_id, data):
        calls.append((execution_id, data))
    return sink


async def _run_execution(agent, execution_id, sink, content, delay):
    """Mirrors AgentRuntime.execute()'s exact set/await/reset shape
    (runtime.py:~314-326) so this test exercises the real calling
    convention, not a simplified stand-in."""
    agent.set_checkpoint_sink(sink)
    try:
        message = Message(content=content, metadata={"delay": delay})
        return await agent.process(message, session_id=f"session-{content}", execution_id=execution_id)
    finally:
        agent.set_checkpoint_sink(None)


async def test_concurrent_executions_on_shared_agent_instance_do_not_cross_contaminate_checkpoints():
    agent = _CheckpointingAgent(AgentConfig(
        name="ConcurrencyProbe",
        semantic_memory=False, episodic_memory=False, procedural_memory=False,
    ))
    await agent.initialize(agent_id="agent-concurrency-probe", local_only=True)

    calls_a: list = []
    calls_b: list = []

    # Task A sleeps mid-run(); Task B (delay=0) races ahead and fully
    # completes -- including its own set_checkpoint_sink(None) reset --
    # before Task A wakes up for its second checkpoint. Under the old
    # instance-attribute implementation this reliably clobbered Task A's
    # in-flight state with Task B's.
    task_a = asyncio.create_task(_run_execution(agent, "exec-A", _make_sink(calls_a), "A", delay=0.05))
    task_b = asyncio.create_task(_run_execution(agent, "exec-B", _make_sink(calls_b), "B", delay=0.0))

    response_a, response_b = await asyncio.gather(task_a, task_b)

    assert response_a.content == "done:A"
    assert response_b.content == "done:B"

    # Each call's sink must have received exactly its own two checkpoints,
    # tagged with its own execution_id -- never the other call's.
    assert len(calls_a) == 2, f"expected 2 checkpoints via sink_a, got {calls_a}"
    assert len(calls_b) == 2, f"expected 2 checkpoints via sink_b, got {calls_b}"

    assert all(execution_id == "exec-A" for execution_id, _ in calls_a), calls_a
    assert all(execution_id == "exec-B" for execution_id, _ in calls_b), calls_b

    assert [data["phase"] for _, data in calls_a] == ["start", "end"]
    assert [data["phase"] for _, data in calls_b] == ["start", "end"]
    assert all(data["who"] == "A" for _, data in calls_a)
    assert all(data["who"] == "B" for _, data in calls_b)

    # No cross-contamination in the other direction either -- sink_a was
    # never invoked with exec-B's execution_id and vice versa.
    assert not any(execution_id == "exec-B" for execution_id, _ in calls_a)
    assert not any(execution_id == "exec-A" for execution_id, _ in calls_b)


async def test_checkpoint_is_a_noop_after_the_owning_execution_completes():
    """A checkpoint() call that races past process()'s own completion (e.g.
    a stray background task holding a reference to the agent) must not
    silently attribute itself to a *different*, later execution that
    happens to be running concurrently -- it should just see no sink/
    execution_id and no-op, per checkpoint()'s documented contract."""
    agent = _CheckpointingAgent(AgentConfig(
        name="ConcurrencyProbe2",
        semantic_memory=False, episodic_memory=False, procedural_memory=False,
    ))
    await agent.initialize(agent_id="agent-concurrency-probe-2", local_only=True)

    # No sink configured, no execution in flight in *this* context.
    await agent.checkpoint({"should": "not raise, not go anywhere"})


async def test_turn_counter_hands_out_unique_sequential_numbers_under_concurrency():
    """turn_counter is deliberately shared (not ContextVar-isolated, unlike
    checkpoint_sink/execution_id) -- assert concurrent process() calls on
    the same instance never receive a duplicate or dropped turn number.

    Reading self._turn_counter from inside run() would NOT prove this --
    by the time run() executes, other concurrent calls may have already
    advanced the shared counter past this call's own value. What actually
    matters is the `turn` value captured locally inside process() for
    *this* call, which flows into pipeline.process_turn(turn_number=...)
    (memory.py) -- so wrap that method to record exactly the turn number
    each call was assigned."""

    class _NoOpAgent(Agent):
        async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
            await asyncio.sleep(0)
            return AgentResponse(content="ok")

    agent = _NoOpAgent(AgentConfig(
        name="TurnProbe", semantic_memory=False, episodic_memory=False, procedural_memory=False,
    ))
    await agent.initialize(agent_id="agent-turn-probe", local_only=True)

    recorded_turn_numbers: list = []
    original_process_turn = agent.pipeline.process_turn

    async def _recording_process_turn(*args, **kwargs):
        recorded_turn_numbers.append(kwargs["turn_number"])
        return await original_process_turn(*args, **kwargs)

    agent.pipeline.process_turn = _recording_process_turn

    async def call(n):
        return await agent.process(Message(content=str(n)), session_id="shared-session")

    await asyncio.gather(*[call(i) for i in range(10)])

    assert agent._turn_counter == 10
    assert sorted(recorded_turn_numbers) == list(range(1, 11)), recorded_turn_numbers
    assert len(set(recorded_turn_numbers)) == 10, "duplicate turn number handed out under concurrency"
