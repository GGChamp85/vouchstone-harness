"""Agent Runtime Core"""

import asyncio
import importlib.util
import os
from typing import Dict, Any, List, Optional
import structlog
import httpx

from .config import Settings
from .bundle import BundleManifest, load_manifest, verify_bundle
from .local_kg import LocalKGStore
from .execution_store import ExecutionStore, STATUS_INTERRUPTED
from .sovereign import enforce_sovereign_mode
import uuid as _uuid

logger = structlog.get_logger()


class AgentRuntime:
    """Core runtime for executing agents"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self._agents: Dict[str, Any] = {}
        self._ready = False
        self._control_plane_client: Optional[httpx.AsyncClient] = None
        # Slice 1 — heartbeat / ledger replay background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._ledger_task: Optional[asyncio.Task] = None
        self._last_spec_sync_ts: float = 0.0
        self._local_queue = None  # lazy init
        # Set when the most recent control-plane agent fetch failed --
        # is_ready() must reflect this rather than reporting healthy with
        # zero agents loaded. Cleared on the next successful fetch.
        self._last_fetch_error: Optional[str] = None
        # C4 — offline harness mode. Set by initialize_from_bundle(); when
        # non-None, execute() and _create_agent_instance() source KG data
        # from here instead of a live control-plane/vector-db connection.
        self._local_kg: Optional[LocalKGStore] = None
        self._offline = False
        # Durable execution tracking (survives a process restart on this
        # disk) -- see execution_store.py. Initialized in both the
        # connected and offline paths so execute() always has it.
        self._execution_store: Optional[ExecutionStore] = None

    async def _init_execution_store(self) -> None:
        self._execution_store = ExecutionStore(self.settings.EXECUTION_STORE_PATH)
        await self._execution_store.initialize()
        interrupted = await self._execution_store.mark_interrupted_on_startup()
        for ex in interrupted:
            logger.warning(
                "execution was still running when the process last stopped -- marked interrupted",
                execution_id=ex["id"], agent_id=ex["agent_id"], started_at=ex["started_at"],
            )

    async def initialize(self):
        """Initialize the runtime. Branches to the fully-offline bundle path
        (C4) when BUNDLE_PATH is configured; otherwise requires
        CONTROL_PLANE_URL and talks to the live control plane.

        Enforces sovereign mode (C7b) first, before either path, if
        enabled -- see sovereign.py. Raises SovereignModeViolation and
        refuses to start entirely rather than starting in a state that
        contradicts the mode a deployer explicitly asked for."""
        await enforce_sovereign_mode(self.settings)

        if self.settings.BUNDLE_PATH:
            await self.initialize_from_bundle(self.settings.BUNDLE_PATH)
            return

        if not self.settings.CONTROL_PLANE_URL:
            raise ValueError(
                "Neither VOUCHSTONE_CONTROL_PLANE_URL nor VOUCHSTONE_BUNDLE_PATH is set. "
                "The runtime needs one of: a live control plane to connect to, or a local "
                "bundle pulled via `vouchstone harness pull` for fully offline operation."
            )

        logger.info("Initializing agent runtime")
        await self._init_execution_store()

        # Connect to control plane
        self._control_plane_client = httpx.AsyncClient(
            base_url=self.settings.CONTROL_PLANE_URL,
            headers={
                "Authorization": f"Bearer {self.settings.API_KEY}",
                "X-Tenant-ID": self.settings.TENANT_ID
            },
            timeout=30.0
        )

        # Load agents from control plane
        try:
            agents = await self._fetch_agents()
            for agent_def in agents:
                if agent_def["status"] == "active":
                    await self.load_agent(agent_def["id"])
            self._last_fetch_error = None
        except Exception as e:
            logger.warning("Could not fetch agents from control plane", error=str(e))
            self._last_fetch_error = str(e)

        # _ready gates the background loops below (they should still run and
        # retry even after a failed initial fetch) -- it is deliberately NOT
        # the same thing as "healthy with agents loaded". is_ready() is the
        # signal callers (e.g. the /ready endpoint) should actually trust.
        self._ready = True
        if self._last_fetch_error:
            logger.warning(
                "Runtime initialized in degraded state -- control plane unreachable, 0 agents loaded",
                error=self._last_fetch_error,
            )
        else:
            logger.info("Runtime initialized", agent_count=len(self._agents))

        # Spin up heartbeat + ledger replay loops (Slice 1).
        try:
            if self.settings.RUNTIME_TOKEN and self.settings.TENANT_ID:
                self._heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                self._ledger_task = asyncio.create_task(self.ledger_replay_loop())
                logger.info("heartbeat + ledger replay loops started")
            else:
                logger.warning(
                    "heartbeat loop disabled — set VOUCHSTONE_RUNTIME_TOKEN and VOUCHSTONE_TENANT_ID"
                )
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("Could not start heartbeat tasks", error=str(e))

    async def initialize_from_bundle(
        self, bundle_dir: str, *,
        public_key_pem: Optional[str] = None, hmac_secret: Optional[str] = None,
    ) -> BundleManifest:
        """Fully offline initialization (C4). Verifies the bundle's hash and
        signature, loads agent definitions and the KG snapshot straight from
        disk, and starts zero background network loops -- no heartbeat, no
        ledger replay, no control-plane client at all. Raises
        BundleVerificationError (not a silent fallback) if the bundle fails
        verification; callers must not catch that and proceed anyway.
        """
        logger.info("Initializing agent runtime from offline bundle", bundle_dir=bundle_dir)
        await self._init_execution_store()

        manifest = verify_bundle(bundle_dir, public_key_pem=public_key_pem, hmac_secret=hmac_secret)

        self._offline = True
        self._control_plane_client = None

        import os as _os
        kg_path = _os.path.join(bundle_dir, manifest.kg_snapshot_filename)
        self._local_kg = LocalKGStore(kg_path)
        await self._local_kg.initialize()

        loaded = 0
        for agent_def in manifest.agents:
            if agent_def.get("status") == "active":
                try:
                    agent_instance = await self._create_agent_instance(agent_def)
                    self._agents[agent_def["id"]] = {"definition": agent_def, "instance": agent_instance}
                    loaded += 1
                except Exception as e:
                    logger.error("Failed to load agent from bundle", agent_id=agent_def.get("id"), error=str(e))
                    raise

        self._last_fetch_error = None
        self._ready = True
        kg_count = await self._local_kg.count()
        logger.info(
            "Runtime initialized from offline bundle -- zero network calls",
            bundle_ref=manifest.bundle_ref, agent_count=loaded, kg_entity_count=kg_count,
        )
        return manifest

    async def _fetch_agents(self) -> List[Dict[str, Any]]:
        """Fetch agent definitions from control plane"""
        response = await self._control_plane_client.get("/api/v1/agents")
        response.raise_for_status()
        return response.json().get("agents", [])
    
    async def load_agent(self, agent_id: str):
        """Load an agent into the runtime"""
        logger.info("Loading agent", agent_id=agent_id)
        
        # Fetch agent definition
        response = await self._control_plane_client.get(f"/api/v1/agents/{agent_id}")
        response.raise_for_status()
        agent_def = response.json()
        
        # Create agent instance
        agent_instance = await self._create_agent_instance(agent_def)
        self._agents[agent_id] = {
            "definition": agent_def,
            "instance": agent_instance
        }
        
        logger.info("Agent loaded", agent_id=agent_id, name=agent_def["name"])
    
    async def _create_agent_instance(self, agent_def: Dict[str, Any]) -> Any:
        """Create an agent instance from definition"""
        from vouchstone_sdk import Agent, AgentConfig

        memory_config = agent_def.get("memory_config", {}) or {}
        # Create config. AgentConfig has no `decision_graph` field -- that
        # was a real, previously-broken bug (this call raised TypeError on
        # every single load_agent()); procedural_memory is the closest real
        # equivalent for "learned skill/decision structure".
        config = AgentConfig(
            name=agent_def["name"],
            model=agent_def.get("config_schema", {}).get("model", self.settings.DEFAULT_MODEL),
            semantic_memory=memory_config.get("semantic", {}).get("enabled", False),
            episodic_memory=memory_config.get("episodic", {}).get("enabled", False),
            procedural_memory=memory_config.get("procedural", {}).get("enabled", False),
        )

        # Create a simple agent implementation
        class RuntimeAgent(Agent):
            async def run(self, message, context):
                import openai
                client = openai.AsyncOpenAI()

                # Build system prompt with context. `context` is a
                # MemoryContext dataclass (no .get()) -- semantic_entities
                # is the real field name (was `context.get("semantic")`,
                # which raised AttributeError on every single call).
                system = self.config.system_prompt or f"You are {self.config.name}, an AI assistant."
                if context.semantic_entities:
                    system += "\n\nRelevant context:\n"
                    for entity in context.semantic_entities[:3]:
                        system += f"- {entity.entity_type}: {entity.entity_key} {entity.attributes}\n"

                response = await client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": message.content}
                    ],
                    temperature=self.config.temperature
                )

                from vouchstone_sdk.types import AgentResponse
                return AgentResponse(
                    content=response.choices[0].message.content,
                    usage={
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens
                    }
                )

        agent = RuntimeAgent(config)
        if self._offline:
            # C4 — fully offline: no vector/graph DB URLs, no api_client,
            # and local_only=True so SemanticMemory never starts an ambient
            # embedded ChromaDB (which would still call out to an embedding
            # provider over the network on every upsert/search -- see
            # SemanticMemory.local_only). ProceduralMemory already stays
            # local with no graph_db_url configured (see C2), no change
            # needed there. Seed semantic memory from the bundle's KG
            # snapshot below so run() actually has something to retrieve.
            runtime_agent_id = str(agent_def.get("id") or agent_def["name"])
            await agent.initialize(agent_id=runtime_agent_id, local_only=True)
            if self._local_kg is not None and config.semantic_memory:
                await self._seed_semantic_memory_from_local_kg(agent, runtime_agent_id)
        else:
            await agent.initialize(
                agent_id=str(agent_def.get("id") or agent_def["name"]),
                vector_db_url=self.settings.VECTOR_DB_URL,
                graph_db_url=self.settings.GRAPH_DB_URL,
            )

        return agent

    async def _seed_semantic_memory_from_local_kg(self, agent, runtime_agent_id: str) -> None:
        """Load every entity from the offline bundle's KG snapshot into the
        agent's semantic memory via upsert_entity() -- the real storage
        method (unlike the legacy store()/search() pair, it maintains the
        local dict that search_entities()'s local_only fallback path
        reads from) -- so run() can retrieve them via the normal
        MemoryContext.semantic_entities path with zero network calls."""
        graph = await self._local_kg.to_entity_graph()
        for entity in graph.all_entities():
            await agent.pipeline.semantic.upsert_entity(runtime_agent_id, entity)
    
    async def unload_agent(self, agent_id: str):
        """Unload an agent from the runtime"""
        if agent_id in self._agents:
            agent_data = self._agents.pop(agent_id)
            await agent_data["instance"].close()
            logger.info("Agent unloaded", agent_id=agent_id)
    
    async def execute(
        self,
        agent_id: str,
        input_data: Dict[str, Any],
        session_id: Optional[str] = None,
        metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Execute an agent. Durably tracked (see execution_store.py) when an
        execution store is available -- status and any checkpoints the
        agent reports via self.checkpoint() during run() survive a process
        restart. execution_id is always returned so a caller can poll
        get_execution_status() for a long-running task instead of just
        waiting on this call."""
        if agent_id not in self._agents:
            raise ValueError(f"Agent {agent_id} not loaded")

        agent = self._agents[agent_id]["instance"]

        from vouchstone_sdk.types import Message
        message = Message(
            content=input_data.get("content", str(input_data)),
            metadata=metadata or {}
        )

        execution_id = str(_uuid.uuid4())
        if self._execution_store is not None:
            await self._execution_store.start_execution(execution_id, agent_id, input_data, session_id)
            agent.set_checkpoint_sink(self._execution_store.checkpoint)

        try:
            response = await agent.process(message, session_id=session_id, execution_id=execution_id)
        except Exception as e:
            if self._execution_store is not None:
                await self._execution_store.fail_execution(execution_id, str(e))
            raise
        finally:
            if self._execution_store is not None:
                agent.set_checkpoint_sink(None)

        result = {
            "output": response.content,
            "session_id": session_id or "default",
            "execution_id": execution_id,
            "metadata": {
                "usage": response.usage,
                "decisions": [d.__dict__ for d in response.decisions]
            }
        }
        if self._execution_store is not None:
            await self._execution_store.complete_execution(execution_id, result)
        return result

    async def get_execution_status(self, execution_id: str) -> Optional[Dict[str, Any]]:
        if self._execution_store is None:
            return None
        return await self._execution_store.get_execution(execution_id)

    async def list_executions(
        self, agent_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if self._execution_store is None:
            return []
        return await self._execution_store.list_executions(agent_id=agent_id, status=status, limit=limit)
    
    def list_agents(self) -> List[Dict[str, Any]]:
        """List loaded agents"""
        return [
            {
                "id": agent_id,
                "name": data["definition"]["name"],
                "status": "loaded"
            }
            for agent_id, data in self._agents.items()
        ]
    
    def is_ready(self) -> bool:
        """True only if the runtime process is alive AND the most recent
        control-plane agent fetch succeeded. A process that's alive but
        couldn't reach the control plane (0 agents loaded as a result) is
        NOT ready -- callers relying on this for a Kubernetes readiness
        probe need it to reflect that degraded state, not report healthy."""
        return self._ready and self._last_fetch_error is None

    def health_detail(self) -> Dict[str, Any]:
        """Diagnostic detail behind is_ready() -- what a /ready endpoint
        should actually return so operators can see *why* it's degraded,
        not just that it is."""
        return {
            "process_alive": self._ready,
            "control_plane_reachable": self._last_fetch_error is None,
            "agent_count": len(self._agents),
            "last_fetch_error": self._last_fetch_error,
        }
    
    # ============================================================
    # Slice 1 — heartbeat + ledger replay + dynamic agent reload
    # ============================================================

    def _heartbeat_base_url(self) -> str:
        return (self.settings.HEARTBEAT_URL or self.settings.CONTROL_PLANE_URL).rstrip("/")

    def _runtime_headers(self) -> Dict[str, str]:
        return {
            "X-Runtime-Token": self.settings.RUNTIME_TOKEN,
            "Content-Type": "application/json",
        }

    def _get_local_queue(self):
        if self._local_queue is None:
            try:
                from .local_queue import LocalQueue
                self._local_queue = LocalQueue(self.settings.LOCAL_QUEUE_PATH)
            except Exception as e:
                logger.warning("Could not init local queue", error=str(e))
        return self._local_queue

    async def heartbeat_loop(self):
        """Send heartbeat every HEARTBEAT_INTERVAL_SECONDS. Apply server hints."""
        interval = max(5, int(self.settings.HEARTBEAT_INTERVAL_SECONDS))
        base = self._heartbeat_base_url()
        client = httpx.AsyncClient(base_url=base, timeout=15.0)
        try:
            while self._ready:
                queue = self._get_local_queue()
                queue_depth = queue.depth() if queue else 0
                body = {
                    "tenant_id": self.settings.TENANT_ID,
                    "runtime_version": self.settings.RUNTIME_VERSION,
                    "pod_count": 1,
                    "queue_depth": queue_depth,
                    "last_ledger_seq_forwarded": 0,
                }
                try:
                    resp = await client.post(
                        "/api/v1/data-plane/heartbeat",
                        json=body, headers=self._runtime_headers(),
                    )
                    if resp.status_code < 300:
                        data = resp.json()
                        for warning in data.get("drift_warnings", []) or []:
                            logger.warning("control plane drift warning", warning=warning)
                        for agent_id in data.get("sync_now_for_agent_ids", []) or []:
                            try:
                                await self.reload_agent(agent_id)
                            except Exception as e:
                                logger.warning("reload_agent failed",
                                               agent_id=agent_id, error=str(e))
                        interval = int(data.get("next_heartbeat_in_seconds", interval) or interval)
                    else:
                        logger.warning("heartbeat non-2xx",
                                       status=resp.status_code, body=resp.text[:200])
                except Exception as e:
                    logger.warning("heartbeat failed", error=str(e))
                await asyncio.sleep(interval)
        finally:
            await client.aclose()

    async def reload_agent(self, agent_id: str):
        """Re-fetch agent spec and rebuild in-memory instance without pod restart."""
        base = self._heartbeat_base_url()
        async with httpx.AsyncClient(base_url=base, timeout=20.0) as client:
            resp = await client.get(
                "/api/v1/data-plane/agents/spec",
                params={
                    "tenant_id": self.settings.TENANT_ID,
                    "since": self._last_spec_sync_ts,
                },
                headers=self._runtime_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        for spec in data.get("agents", []) or []:
            if str(spec.get("id")) != str(agent_id):
                continue
            try:
                if agent_id in self._agents:
                    await self.unload_agent(agent_id)
            except Exception:
                pass
            try:
                # Convert spec to the legacy agent_def shape consumed by _create_agent_instance.
                agent_def = {
                    "id": spec["id"],
                    "name": spec.get("name") or "agent",
                    "status": "active",
                    "memory_config": {},
                    "config_schema": {
                        "model": (spec.get("definition") or {}).get(
                            "llm_model", self.settings.DEFAULT_MODEL
                        ),
                    },
                }
                instance = await self._create_agent_instance(agent_def)
                self._agents[agent_id] = {"definition": agent_def, "instance": instance}
                logger.info("Agent reloaded dynamically", agent_id=agent_id)
            except Exception as e:
                logger.warning("Agent reload failed", agent_id=agent_id, error=str(e))

        import time as _t
        self._last_spec_sync_ts = _t.time()

    async def ledger_replay_loop(self):
        """Drain locally-queued ledger entries to the control plane every N seconds."""
        interval = max(15, int(self.settings.LEDGER_REPLAY_INTERVAL_SECONDS))
        base = self._heartbeat_base_url()
        client = httpx.AsyncClient(base_url=base, timeout=20.0)
        try:
            while self._ready:
                try:
                    queue = self._get_local_queue()
                    if queue is not None:
                        entries = queue.peek(limit=100)
                        if entries:
                            payload_entries = []
                            ids = []
                            for e in entries:
                                ids.append(e.pop("_queue_id"))
                                payload_entries.append(e)
                            resp = await client.post(
                                "/api/v1/data-plane/ledger/replay",
                                json={
                                    "tenant_id": self.settings.TENANT_ID,
                                    "entries": payload_entries,
                                },
                                headers=self._runtime_headers(),
                            )
                            if resp.status_code < 300:
                                queue.mark_forwarded(ids)
                                logger.info("ledger replay forwarded",
                                            count=len(ids))
                            else:
                                logger.warning(
                                    "ledger replay non-2xx",
                                    status=resp.status_code, body=resp.text[:200],
                                )
                        queue.prune_old()
                except Exception as e:
                    logger.warning("ledger replay loop error", error=str(e))
                await asyncio.sleep(interval)
        finally:
            await client.aclose()

    async def shutdown(self):
        """Shutdown the runtime"""
        logger.info("Shutting down runtime")

        self._ready = False

        # Cancel background loops
        for task in (self._heartbeat_task, self._ledger_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Unload all agents
        for agent_id in list(self._agents.keys()):
            await self.unload_agent(agent_id)

        # Close control plane client
        if self._control_plane_client:
            await self._control_plane_client.aclose()

        if self._local_kg is not None:
            await self._local_kg.close()
        if self._execution_store is not None:
            await self._execution_store.close()
