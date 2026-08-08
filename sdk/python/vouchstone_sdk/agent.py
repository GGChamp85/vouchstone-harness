"""Agent Base Class with 5-Layer Memory Pipeline"""

import asyncio
import contextvars
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .memory import MemoryPipeline
from .telemetry import record_exception, span
from .types import AgentResponse, MemoryContext, Message

# AgentRuntime keeps exactly one Agent instance per agent_id and reuses it
# across every call to that agent (runtime.py's self._agents dict) -- so
# two overlapping /execute requests for the same agent_id share this same
# Python object. Plain instance attributes (self._checkpoint_sink,
# self._current_execution_id) are therefore shared mutable state across
# those concurrent calls, and process() has real `await` points between
# writing them and the code that reads them (prepare_context(), run()),
# so one call's in-flight state can be clobbered by another's.
#
# contextvars.ContextVar fixes this correctly (unlike a plain instance
# attribute, or a lock, or a local variable): each incoming request is
# handled in its own asyncio Task (that's how Starlette/FastAPI and
# asyncio.create_task() both work), and each Task gets its own copy of the
# current context the moment it's created. A `.set()` made while running
# inside one Task is invisible to a concurrently-running sibling Task, but
# *is* visible across ordinary `await` calls within the same Task's own
# call chain (execute() -> process() -> run() -> checkpoint()) -- exactly
# the isolation these two fields need. Module-level is correct even though
# multiple Agent instances (different agent_ids) share the same ContextVar
# objects: the *value* stored is still per-context (per in-flight call),
# not per Agent instance, so there's no cross-agent leakage either.
_checkpoint_sink_var: "contextvars.ContextVar[Callable[[str, dict[str, Any]], Awaitable[None]] | None]" = (
    contextvars.ContextVar("vouchstone_checkpoint_sink", default=None)
)
_current_execution_id_var: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "vouchstone_current_execution_id", default=None
)


@dataclass
class AgentConfig:
    """Configuration for a Vouchstone Agent"""
    name: str
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: str | None = None

    working_memory: bool = True
    semantic_memory: bool = True
    episodic_memory: bool = True
    procedural_memory: bool = True
    meta_memory: bool = True

    embedding_model: str = "text-embedding-3-small"
    memory_retention_days: int = 90

    tools: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Sub-graph scoping ("Specialize") -- constrains which semantic
    # entities and procedural skills this agent's memory queries can see.
    # None (the default) means unscoped: the agent can see everything in
    # its own agent_id namespace, same as before this field existed.
    # Entries match Entity.entity_type / a Skill's tags (see Skill.tags in
    # types.py) -- e.g. ["process", "system", "document"] for an
    # AP-invoice agent that should never surface unrelated "person" or
    # "regulation" entities from elsewhere in the company's graph.
    scoped_subgraph: list[str] | None = None

    # KG-domain scoping (Phase 3) -- a *different* axis than
    # scoped_subgraph above: entries here are kg_domains registry slugs
    # (e.g. "finance", "it", "compliance" -- the same taxonomy
    # app/services/kg_domains.py and the control plane's Agent.kg_scope /
    # Participant.kg_scope use), not entity types or free-form skill tags.
    # None means unscoped. Matched against ``Entity.attributes["domains"]``
    # (a list of slugs, mirroring KGNode.domains) and, for skills, against
    # ``Skill.tags`` (skills have no separate domains field -- a skill
    # "belongs" to a domain by carrying that slug alongside its other
    # tags). This is a FLAT match only: unlike the control plane's
    # _matches_kg_scope, the SDK does not expand a scope like ["it"] to
    # also match a node/skill tagged only "code" or "security" -- that
    # hierarchy expansion needs the kg_domains registry (a control-plane
    # concept with no offline equivalent here), so it must happen
    # server-side before scoped_domains is set (e.g. when an offline
    # harness bundle or a data-plane execution request is built, the
    # caller resolves the agent's kg_scope.domains through the registry
    # first and passes the already-expanded slug set here).
    scoped_domains: list[str] | None = None


class Agent(ABC):
    """Base class for Vouchstone Agents with 5-Layer Memory Pipeline.

    Subclass this and implement ``run()`` to build a custom agent.
    The pipeline handles memory context preparation and post-turn
    persistence automatically.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.pipeline: MemoryPipeline | None = None
        self._tools: dict[str, Callable] = {}
        self._initialized = False
        self._session_id: str | None = None
        # checkpoint_sink and current_execution_id are NOT instance
        # attributes -- see the module-level ContextVars above and
        # set_checkpoint_sink()/checkpoint()/process() below. They're
        # call-scoped (per in-flight execution), not per-Agent-instance,
        # because AgentRuntime shares one Agent instance across concurrent
        # calls to the same agent_id.
        #
        # _turn_counter is different: it's a genuinely *shared*, ordered
        # sequence number across every call this instance handles, so it
        # stays a real instance attribute -- just guarded by a lock (see
        # process()) instead of racing on an unguarded read-modify-write.
        self._turn_counter: int = 0
        self._turn_lock = asyncio.Lock()

    async def initialize(
        self,
        agent_id: str | None = None,
        redis_url: str | None = None,
        vector_db_url: str | None = None,
        graph_db_url: str | None = None,
        api_client=None,
        local_only: bool = False,
    ):
        """``local_only=True`` (offline harness mode, C4) disables semantic
        memory's vector search entirely rather than letting it fall back to
        an ambient embedded ChromaDB -- that fallback still calls out to an
        embedding provider over the network for every upsert/search, which
        a genuinely offline agent must not do. Falls back to local
        substring matching instead (see SemanticMemory.local_only)."""
        self._agent_id = agent_id or str(uuid.uuid4())
        self.pipeline = MemoryPipeline(
            agent_id=self._agent_id,
            redis_url=redis_url,
            vector_db_url=vector_db_url,
            graph_db_url=graph_db_url,
            api_client=api_client,
            scoped_subgraph=self.config.scoped_subgraph,
            scoped_domains=self.config.scoped_domains,
            local_only=local_only,
            # The five AgentConfig layer booleans, embedding_model, and
            # memory_retention_days are honored here -- historically they
            # were documented config that nothing read.
            enabled_layers={
                "working": self.config.working_memory,
                "episodic": self.config.episodic_memory,
                "semantic": self.config.semantic_memory,
                "procedural": self.config.procedural_memory,
                "meta": self.config.meta_memory,
            },
            embedding_model=self.config.embedding_model,
            retention_days=self.config.memory_retention_days,
        )
        await self.pipeline.initialize()
        self._initialized = True

    def start_session(self, session_id: str | None = None) -> str:
        self._session_id = session_id or str(uuid.uuid4())
        return self._session_id

    def set_checkpoint_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None) -> None:
        """Called by whoever drives execution (AgentRuntime.execute()) to
        wire checkpoint() up to durable storage. sink receives
        (execution_id, data).

        Stored in a contextvars.ContextVar, not an instance attribute --
        see the module-level comment above. AgentRuntime.execute() calls
        this once before ``await agent.process(...)`` and again with
        ``None`` in its ``finally`` (both from within the same asyncio
        Task), so the set/clear pair is scoped to that one execution and
        invisible to any concurrent execute() call for the same agent_id
        running in a different Task."""
        _checkpoint_sink_var.set(sink)

    async def checkpoint(self, data: dict[str, Any]) -> None:
        """Call from inside run() during a long-running task to report
        progress that survives a process restart -- e.g.
        ``await self.checkpoint({"step": "3/10", "rows_processed": 4200})``.
        No-ops if no execution is in flight or no sink is configured (e.g.
        the agent is being used directly via the SDK, not through
        AgentRuntime) -- never raises just because nothing is listening."""
        sink = _checkpoint_sink_var.get()
        execution_id = _current_execution_id_var.get()
        if sink is not None and execution_id is not None:
            await sink(execution_id, data)

    def register_tool(self, name: str, func: Callable, description: str):
        self._tools[name] = func
        self.config.tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        })

    async def process(
        self, message: Message, session_id: str | None = None,
        execution_id: str | None = None,
    ) -> AgentResponse:
        if not self._initialized or self.pipeline is None:
            raise RuntimeError("Agent not initialized. Call initialize() first.")
        # Bind once so the None-check above holds for the whole call even if
        # close() nulls self.pipeline concurrently.
        pipeline = self.pipeline

        sid = session_id or self._session_id or self.start_session()

        # Atomic read-modify-write of a *shared* sequence counter across
        # every call this Agent instance handles. Guarded with a lock
        # rather than isolated via ContextVar on purpose: unlike
        # checkpoint_sink/current_execution_id above, turn_counter's job is
        # to hand out a globally-increasing number across calls, so
        # per-call isolation would be the wrong fix here -- every
        # concurrent call would just compute turn=1 and the counter would
        # never actually advance. Note this statement pair was already
        # non-preemptible under single-threaded asyncio (no `await` between
        # the read and the write), so this lock is defense-in-depth against
        # a future refactor introducing one, not a fix for an observed
        # corruption of the counter itself.
        async with self._turn_lock:
            turn = self._turn_counter + 1
            self._turn_counter = turn

        # Scoped via a ContextVar (not an instance attribute) so
        # checkpoint() attributes to the right execution even when this
        # Agent instance is handling another overlapping process() call
        # concurrently -- see the module-level comment above and
        # set_checkpoint_sink(). token.reset() (rather than setting back to
        # None) restores whatever value was present before this call in
        # this context, which matters if process() is ever invoked
        # re-entrantly within the same call chain.
        execution_id_token = _current_execution_id_var.set(execution_id)
        with span("vouchstone.agent.process", {
            "vouchstone.agent.name": self.config.name,
            "vouchstone.agent.id": self._agent_id,
            "vouchstone.session_id": sid,
            "vouchstone.turn_number": turn,
        }) as current_span:
            try:
                context = await pipeline.prepare_context(sid, message.content)
                response = await self.run(message, context)

                await pipeline.process_turn(
                    session_id=sid,
                    turn_number=turn,
                    user_input=message.content,
                    agent_response=response.content,
                    tools_used=[tc.get("function", {}).get("name", "") for tc in response.tool_calls],
                    tokens_in=response.usage.get("prompt_tokens", 0),
                    tokens_out=response.usage.get("completion_tokens", 0),
                    latency_ms=response.metadata.get("latency_ms", 0),
                    success=True,
                )
            except Exception as exc:
                record_exception(current_span, exc)
                raise
            finally:
                _current_execution_id_var.reset(execution_id_token)

        return response

    async def end_session(self, session_id: str | None = None):
        sid = session_id or self._session_id
        if sid and self.pipeline:
            await self.pipeline.end_session(sid)

    @abstractmethod
    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        """Implement agent logic. Override this in your subclass."""
        pass

    async def close(self):
        if self.pipeline:
            await self.pipeline.close()
