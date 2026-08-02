"""Agent Base Class with 5-Layer Memory Pipeline"""

from typing import Optional, Dict, Any, List, Callable, Awaitable
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import asyncio
import uuid

from .memory import MemoryPipeline, SemanticMemory, EpisodicMemory, ProceduralMemory
from .types import Message, AgentResponse, MemoryContext
from .telemetry import record_exception, span


@dataclass
class AgentConfig:
    """Configuration for a Vouchstone Agent"""
    name: str
    model: str = "claude-sonnet-4-20250514"
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: Optional[str] = None

    working_memory: bool = True
    semantic_memory: bool = True
    episodic_memory: bool = True
    procedural_memory: bool = True
    meta_memory: bool = True

    embedding_model: str = "text-embedding-3-small"
    memory_retention_days: int = 90

    tools: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Sub-graph scoping ("Specialize") -- constrains which semantic
    # entities and procedural skills this agent's memory queries can see.
    # None (the default) means unscoped: the agent can see everything in
    # its own agent_id namespace, same as before this field existed.
    # Entries match Entity.entity_type / a Skill's tags (see Skill.tags in
    # types.py) -- e.g. ["process", "system", "document"] for an
    # AP-invoice agent that should never surface unrelated "person" or
    # "regulation" entities from elsewhere in the company's graph.
    scoped_subgraph: Optional[List[str]] = None


class Agent(ABC):
    """Base class for Vouchstone Agents with 5-Layer Memory Pipeline.

    Subclass this and implement ``run()`` to build a custom agent.
    The pipeline handles memory context preparation and post-turn
    persistence automatically.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.pipeline: Optional[MemoryPipeline] = None
        self._tools: Dict[str, Callable] = {}
        self._initialized = False
        self._session_id: Optional[str] = None
        # Set by whoever drives execution (e.g. AgentRuntime) via
        # set_checkpoint_sink() -- lets a long-running run() report
        # progress that survives a process restart. Unset by default: an
        # agent used standalone via the SDK directly doesn't need this and
        # checkpoint() silently no-ops. See execution_store.py.
        self._checkpoint_sink: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None
        self._current_execution_id: Optional[str] = None

    async def initialize(
        self,
        agent_id: Optional[str] = None,
        redis_url: Optional[str] = None,
        vector_db_url: Optional[str] = None,
        graph_db_url: Optional[str] = None,
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
            local_only=local_only,
        )
        await self.pipeline.initialize()
        self._initialized = True

    def start_session(self, session_id: Optional[str] = None) -> str:
        self._session_id = session_id or str(uuid.uuid4())
        return self._session_id

    def set_checkpoint_sink(self, sink: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]]) -> None:
        """Called by whoever drives execution (AgentRuntime.execute()) to
        wire checkpoint() up to durable storage. sink receives
        (execution_id, data)."""
        self._checkpoint_sink = sink

    async def checkpoint(self, data: Dict[str, Any]) -> None:
        """Call from inside run() during a long-running task to report
        progress that survives a process restart -- e.g.
        ``await self.checkpoint({"step": "3/10", "rows_processed": 4200})``.
        No-ops if no execution is in flight or no sink is configured (e.g.
        the agent is being used directly via the SDK, not through
        AgentRuntime) -- never raises just because nothing is listening."""
        if self._checkpoint_sink is not None and self._current_execution_id is not None:
            await self._checkpoint_sink(self._current_execution_id, data)

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
        self, message: Message, session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> AgentResponse:
        if not self._initialized:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

        sid = session_id or self._session_id or self.start_session()
        turn = getattr(self, "_turn_counter", 0) + 1
        self._turn_counter = turn

        # Scoped to this call so checkpoint() attributes to the right
        # execution even if this Agent instance handles calls sequentially
        # for different executions. Not safe for concurrent overlapping
        # process() calls on the same instance -- match AgentRuntime's
        # existing one-instance-per-agent-id assumption.
        self._current_execution_id = execution_id
        with span("vouchstone.agent.process", {
            "vouchstone.agent.name": self.config.name,
            "vouchstone.agent.id": self._agent_id,
            "vouchstone.session_id": sid,
            "vouchstone.turn_number": turn,
        }) as current_span:
            try:
                context = await self.pipeline.prepare_context(sid, message.content)
                response = await self.run(message, context)

                await self.pipeline.process_turn(
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
                self._current_execution_id = None

        return response

    async def end_session(self, session_id: Optional[str] = None):
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
