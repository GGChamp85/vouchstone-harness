"""
5-Layer Agent Memory Stack for Vouchstone Agents

Architecture (biologically inspired):
  Layer 1 — Working Memory (Redis): current turn context, resets per session
  Layer 2 — Episodic Memory (PostgreSQL): cross-session traces, append-only
  Layer 3 — Semantic Memory (ChromaDB): entity store with vector search
  Layer 4 — Procedural Memory (Neo4j): learned skills as versioned DAG
  Layer 5 — Meta-Memory (Control Plane): decay, dedup, compress, forget

Runtime pipeline:
  User input → working memory append → LLM planning (reads layers 2-4)
  → tool execution → response → after-turn: episodic append (sync),
  semantic extraction (async), procedural reflection (async batch).
  Meta runs on a scheduled basis.
"""

import asyncio
import hashlib
import json
import logging
from typing import Any

from .types import (
    Entity,
    EpisodicTrace,
    HealthReport,
    MemoryContext,
    MemoryEntry,
    Skill,
    TurnResult,
)

logger = logging.getLogger(__name__)


class MemoryBackendUnavailableError(RuntimeError):
    """Raised when a memory layer was given an explicit backend URL
    (redis_url / vector_db_url / graph_db_url) but couldn't connect to it.

    An explicit URL is a durability requirement from the caller -- silently
    falling back to non-persistent in-process storage in that case means
    the caller believes their agent's memory survives a restart when it
    doesn't. When no URL is configured at all, local in-process storage is
    the documented, intentional default and does not raise this.
    """


class WorkingMemory:
    """Layer 1 — Per-session scratchpad backed by Redis.

    Holds the current turn's context window. Resets when the session ends.
    Falls back to in-memory dict when no Redis URL is provided.
    """

    SESSION_TTL = 3600
    MAX_ENTRIES = 200

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url
        self._redis: Any = None
        self._local: dict[str, list[dict[str, Any]]] = {}
        self._scratchpads: dict[str, dict[str, Any]] = {}

    async def initialize(self):
        if self.redis_url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception as e:
                self._redis = None
                raise MemoryBackendUnavailableError(
                    f"WorkingMemory: redis_url was set but Redis is unreachable "
                    f"({e}). Not silently falling back to non-persistent local "
                    f"storage -- fix the connection or omit redis_url to use "
                    f"local storage explicitly."
                ) from e

    def _key(self, agent_id: str, session_id: str) -> str:
        return f"wm:{agent_id}:{session_id}"

    async def append(self, agent_id: str, session_id: str, role: str, content: str,
                     metadata: dict[str, Any] | None = None):
        entry = {"role": role, "content": content, "metadata": metadata or {}}
        if self._redis:
            key = self._key(agent_id, session_id)
            await self._redis.rpush(key, json.dumps(entry))
            await self._redis.ltrim(key, -self.MAX_ENTRIES, -1)
            await self._redis.expire(key, self.SESSION_TTL)
        else:
            key = self._key(agent_id, session_id)
            self._local.setdefault(key, []).append(entry)
            self._local[key] = self._local[key][-self.MAX_ENTRIES:]

    async def get_context(self, agent_id: str, session_id: str,
                          max_tokens: int = 4000) -> list[dict[str, Any]]:
        if self._redis:
            key = self._key(agent_id, session_id)
            raw = await self._redis.lrange(key, 0, -1)
            entries = [json.loads(r) for r in raw]
        else:
            key = self._key(agent_id, session_id)
            entries = list(self._local.get(key, []))

        result: list[dict[str, Any]] = []
        budget = max_tokens
        for entry in reversed(entries):
            cost = len(entry["content"]) // 4
            if cost > budget:
                break
            result.insert(0, entry)
            budget -= cost
        return result

    async def set_scratchpad(self, agent_id: str, session_id: str,
                             data: dict[str, Any]):
        if self._redis:
            key = f"scratch:{agent_id}:{session_id}"
            await self._redis.set(key, json.dumps(data), ex=self.SESSION_TTL)
        else:
            self._scratchpads[f"{agent_id}:{session_id}"] = data

    async def get_scratchpad(self, agent_id: str, session_id: str) -> dict[str, Any]:
        if self._redis:
            key = f"scratch:{agent_id}:{session_id}"
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else {}
        return self._scratchpads.get(f"{agent_id}:{session_id}", {})

    async def clear_session(self, agent_id: str, session_id: str):
        if self._redis:
            await self._redis.delete(
                self._key(agent_id, session_id),
                f"scratch:{agent_id}:{session_id}",
            )
        else:
            key = self._key(agent_id, session_id)
            self._local.pop(key, None)
            self._scratchpads.pop(f"{agent_id}:{session_id}", None)

    async def close(self):
        if self._redis:
            await self._redis.close()


class EpisodicMemory:
    """Layer 2 — Append-only trace log backed by the control plane API.

    Each turn produces an EpisodicTrace with importance scoring.
    Falls back to local list when no API client is available.
    """

    def __init__(self, api_client=None, retention_days: int = 90):
        self.api_client = api_client
        self.retention_days = retention_days
        self._local_traces: list[EpisodicTrace] = []

    async def initialize(self):
        pass

    def _compute_importance(self, trace: EpisodicTrace) -> float:
        score = 0.5
        if trace.success:
            score += 0.1
        if trace.tools_used:
            score += min(len(trace.tools_used) * 0.05, 0.2)
        if trace.tokens_out > 200:
            score += 0.1
        return min(score, 1.0)

    async def append_trace(self, agent_id: str, trace: EpisodicTrace) -> str:
        trace.importance = self._compute_importance(trace)
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/process-turn", {
                "agent_id": agent_id,
                "session_id": trace.session_id,
                "turn_number": trace.turn_number,
                "user_input": trace.user_input,
                "agent_response": trace.agent_response,
                "tools_used": trace.tools_used,
                "tokens_in": trace.tokens_in,
                "tokens_out": trace.tokens_out,
                "latency_ms": trace.latency_ms,
                "success": trace.success,
            })
            return resp.get("episodic_trace_id", trace.id)
        self._local_traces.append(trace)
        return trace.id

    async def get_recent(self, agent_id: str, session_id: str | None = None,
                         limit: int = 20) -> list[EpisodicTrace]:
        if self.api_client:
            params: dict[str, Any] = {"limit": limit}
            if session_id:
                params["session_id"] = session_id
            resp = await self.api_client._get(
                f"/memory-pipeline/snapshot/{agent_id}", params=params
            )
            return resp.get("episodic", [])

        traces = self._local_traces
        if session_id:
            traces = [t for t in traces if t.session_id == session_id]
        return sorted(traces, key=lambda t: t.timestamp, reverse=True)[:limit]

    async def search(self, query: str, limit: int = 5,
                     agent_id: str | None = None) -> list[EpisodicTrace]:
        """Substring search over episodic traces.

        With an api_client configured, traces live server-side -- pull the
        agent's recent window from the control plane's snapshot endpoint and
        filter it here (there is no dedicated server-side episodic-search
        route). Previously this method silently ignored api_client and only
        searched the local list, so a control-plane-backed agent always got
        [] back. ``agent_id`` is required for the server-side path.
        """
        q = query.lower()

        def _matches(user_input: str, agent_response: str) -> bool:
            return q in user_input.lower() or q in agent_response.lower()

        if self.api_client:
            if not agent_id:
                raise ValueError(
                    "EpisodicMemory.search() needs agent_id when backed by "
                    "the control plane -- traces are stored per-agent "
                    "server-side."
                )
            resp = await self.api_client._get(
                f"/memory-pipeline/snapshot/{agent_id}", params={"limit": max(limit * 10, 50)}
            )
            out: list[EpisodicTrace] = []
            for t in resp.get("episodic", []):
                if isinstance(t, dict) and _matches(
                    t.get("user_input", ""), t.get("agent_response", "")
                ):
                    out.append(t)  # type: ignore[arg-type]
                if len(out) >= limit:
                    break
            return out

        matches = [
            t for t in self._local_traces
            if _matches(t.user_input, t.agent_response)
        ]
        return matches[:limit]

    async def close(self):
        pass


class SemanticMemory:
    """Layer 3 — Entity store with vector search (ChromaDB).

    Stores typed entities (person, technology, system, process, etc.)
    with attributes and confidence scores. Supports upsert-with-merge.
    """

    ENTITY_TYPES = [
        "person", "organization", "system", "technology", "process",
        "concept", "location", "event", "product", "metric",
        "regulation", "skill", "document", "api", "database",
    ]

    def __init__(
        self,
        embedding_model: str = "text-embedding-3-small",
        vector_db_url: str | None = None,
        collection_name: str = "semantic_memory",
        api_client=None,
        local_only: bool = False,
    ):
        self.embedding_model = embedding_model
        self.vector_db_url = vector_db_url
        self.collection_name = collection_name
        self.api_client = api_client
        # Explicit opt-out of vector search entirely (offline harness mode,
        # C4). Embedded Chroma (the ambient no-config path below) still
        # calls out to an embedding provider (OpenAI) for every upsert/
        # search -- that's a real network dependency a fully offline agent
        # must not have. local_only skips starting Chroma at all and goes
        # straight to the local-dict substring-match path, which has no
        # network dependency. This is a deliberate mode, not a degraded
        # fallback from a failed connection.
        self.local_only = local_only
        self._chroma_client: Any = None
        self._embedder = None
        self._local_entities: dict[str, Entity] = {}
        # Set when no vector_db_url was configured and the local embedded
        # Chroma client also couldn't be started -- search_entities() falls
        # back to plain substring matching with no vector search at all.
        # Unlike a configured backend being unreachable, this doesn't raise
        # (no explicit durability promise was made), but it must be visible
        # to callers, not silent -- see MemoryPipeline.health_detail().
        self.degraded = False
        self.degraded_reason: str | None = None

    async def initialize(self):
        if self.local_only:
            self.degraded = True
            self.degraded_reason = "local_only mode: vector search disabled by design (no network embedding calls)"
            return
        if self.vector_db_url:
            try:
                import chromadb
                self._chroma_client = chromadb.HttpClient(host=self.vector_db_url)
                self._chroma_client.heartbeat()
            except Exception as e:
                self._chroma_client = None
                raise MemoryBackendUnavailableError(
                    f"SemanticMemory: vector_db_url was set but ChromaDB is "
                    f"unreachable ({e}). Not silently falling back to "
                    f"non-vector local search."
                ) from e
        elif not self.api_client:
            try:
                import chromadb
                self._chroma_client = chromadb.Client()
            except Exception as e:
                self._chroma_client = None
                self.degraded = True
                self.degraded_reason = f"local embedded ChromaDB unavailable: {e}"
                logger.warning(
                    "SemanticMemory running degraded: no vector_db_url configured and "
                    "local embedded ChromaDB failed to start (%s). search_entities() "
                    "will fall back to plain substring matching, not vector search.",
                    e,
                )

    async def upsert_entity(self, agent_id: str, entity: Entity) -> str:
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/entities/upsert", {
                "agent_id": agent_id,
                "entity_type": entity.entity_type,
                "entity_key": entity.entity_key,
                "attributes": entity.attributes,
                "confidence": entity.confidence,
            })
            return resp.get("id", entity.id)

        key = f"{agent_id}:{entity.entity_type}:{entity.entity_key}"
        if key in self._local_entities:
            existing = self._local_entities[key]
            existing.attributes.update(entity.attributes)
            existing.confidence = max(existing.confidence, entity.confidence)
        else:
            self._local_entities[key] = entity

        if self._chroma_client:
            await self._upsert_vector(agent_id, entity)
        return entity.id

    async def search_entities(self, agent_id: str, query: str,
                              top_k: int = 10,
                              allowed_types: list[str] | None = None,
                              allowed_domains: list[str] | None = None) -> list[Entity]:
        """``allowed_types`` implements AgentConfig.scoped_subgraph ("Specialize")
        -- when set, results are filtered to only those entity types.
        ``allowed_domains`` implements AgentConfig.scoped_domains -- when
        set, results are filtered to entities whose
        ``attributes["domains"]`` (a list of kg_domains slugs, mirroring
        the control plane's ``KGNode.domains``) intersects it. Both are
        applied uniformly after fetch regardless of which backend served
        the query (api_client, vector search, or local dict), so scoping
        can't be silently bypassed by whichever backend happens to be
        active."""
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/entities/search", {
                "agent_id": agent_id,
                "query": query,
                "top_k": top_k,
            })
            results = resp.get("entities", [])
        elif self._chroma_client:
            results = await self._vector_search(agent_id, query, top_k)
        else:
            q = query.lower()
            results = [
                e for key, e in self._local_entities.items()
                if key.startswith(f"{agent_id}:") and q in e.entity_key.lower()
            ][:top_k]

        return self._apply_scope(self._apply_domain_scope(results, allowed_domains), allowed_types)

    async def list_entities(self, agent_id: str,
                            entity_type: str | None = None,
                            allowed_types: list[str] | None = None,
                            allowed_domains: list[str] | None = None) -> list[Entity]:
        if self.api_client:
            resp = await self.api_client._get(
                f"/memory-pipeline/entities/{agent_id}",
                params={"entity_type": entity_type} if entity_type else {},
            )
            results = resp.get("entities", [])
        else:
            results = [
                e for key, e in self._local_entities.items()
                if key.startswith(f"{agent_id}:")
            ]
            if entity_type:
                results = [e for e in results if e.entity_type == entity_type]

        return self._apply_scope(self._apply_domain_scope(results, allowed_domains), allowed_types)

    @staticmethod
    def _apply_scope(entities: list[Entity], allowed_types: list[str] | None) -> list[Entity]:
        if not allowed_types:
            return entities
        allowed = set(allowed_types)
        return [e for e in entities if getattr(e, "entity_type", None) in allowed]

    @staticmethod
    def _apply_domain_scope(entities: list[Entity], allowed_domains: list[str] | None) -> list[Entity]:
        """Flat kg_domains-slug filter -- see AgentConfig.scoped_domains for
        why this doesn't do hierarchy expansion the way the control plane's
        _matches_kg_scope does."""
        if not allowed_domains:
            return entities
        allowed = set(allowed_domains)
        out = []
        for e in entities:
            node_domains = set((getattr(e, "attributes", None) or {}).get("domains") or [])
            if node_domains & allowed:
                out.append(e)
        return out

    async def _upsert_vector(self, agent_id: str, entity: Entity):
        collection = self._chroma_client.get_or_create_collection(
            f"{self.collection_name}_{agent_id}"
        )
        text = f"{entity.entity_type}: {entity.entity_key} — {json.dumps(entity.attributes)}"
        embedding = await self._get_embedding(text)
        collection.upsert(
            ids=[entity.id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{"type": entity.entity_type, "key": entity.entity_key}],
        )

    async def _vector_search(self, agent_id: str, query: str,
                             top_k: int) -> list[Entity]:
        try:
            collection = self._chroma_client.get_collection(
                f"{self.collection_name}_{agent_id}"
            )
        except Exception:
            return []
        embedding = await self._get_embedding(query)
        results = collection.query(query_embeddings=[embedding], n_results=top_k)
        entities = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results.get("metadatas") else {}
            entities.append(Entity(
                id=results["ids"][0][i],
                entity_type=meta.get("type", "unknown"),
                entity_key=meta.get("key", ""),
                attributes={"raw": doc},
            ))
        return entities

    async def _get_embedding(self, text: str) -> list[float]:
        """Raises on failure -- a fake zero-vector would silently corrupt
        every downstream cosine-similarity search (a zero-vector isn't "no
        opinion", it produces meaningless/degenerate similarity scores that
        look like real results). Callers must handle the failure, not
        receive corrupted data that looks valid."""
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "Semantic-memory embeddings require the OpenAI client. "
                "Install it with: pip install 'vouchstone-sdk[llm-openai]'"
            ) from exc
        client = openai.AsyncOpenAI()
        response = await client.embeddings.create(
            model=self.embedding_model, input=text
        )
        return response.data[0].embedding

    # Legacy compatibility
    async def store(self, text: str, metadata: dict[str, Any] | None = None) -> str:
        doc_id = hashlib.md5(text.encode()).hexdigest()
        if self._chroma_client:
            embedding = await self._get_embedding(text)
            collection = self._chroma_client.get_or_create_collection(self.collection_name)
            collection.add(
                ids=[doc_id], embeddings=[embedding],
                documents=[text], metadatas=[metadata or {}],
            )
        return doc_id

    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        if self._chroma_client:
            embedding = await self._get_embedding(query)
            collection = self._chroma_client.get_or_create_collection(self.collection_name)
            results = collection.query(query_embeddings=[embedding], n_results=top_k)
            entries = []
            for i, doc in enumerate(results["documents"][0]):
                entries.append(MemoryEntry(
                    id=results["ids"][0][i],
                    content=doc,
                    metadata=results["metadatas"][0][i] if results.get("metadatas") else {},
                    score=results["distances"][0][i] if results.get("distances") else 0.0,
                ))
            return entries
        return []

    async def close(self):
        pass


class ProceduralMemory:
    """Layer 4 — Learned skills stored as a versioned DAG (Neo4j).

    Records skill definitions with steps, tools, prerequisites, and
    tracks execution success rate + average latency over time.
    """

    def __init__(self, graph_db_url: str | None = None, api_client=None):
        self.graph_db_url = graph_db_url
        self.api_client = api_client
        self._driver: Any = None
        self._pg_pool: Any = None
        self._local_skills: dict[str, Skill] = {}
        # Count of skills whose graph persistence failed on BOTH paths --
        # surfaced so operators can alert on durability loss instead of
        # discovering it after a restart. See _upsert_graph_node_pg.
        self.graph_write_errors: int = 0

    async def initialize(self):
        if self.graph_db_url:
            if self.graph_db_url.startswith(("postgres://", "postgresql://")):
                try:
                    import asyncpg
                    self._pg_pool = await asyncpg.create_pool(self.graph_db_url)
                    async with self._pg_pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                except Exception as e:
                    self._pg_pool = None
                    raise MemoryBackendUnavailableError(
                        f"ProceduralMemory: graph_db_url was set (postgres) but "
                        f"unreachable ({e}). Not silently falling back to "
                        f"non-persistent local skill storage."
                    ) from e
            else:
                try:
                    from neo4j import AsyncGraphDatabase
                    self._driver = AsyncGraphDatabase.driver(self.graph_db_url)
                    await self._driver.verify_connectivity()
                except Exception as e:
                    self._driver = None
                    raise MemoryBackendUnavailableError(
                        f"ProceduralMemory: graph_db_url was set (neo4j) but "
                        f"unreachable ({e}). Not silently falling back to "
                        f"non-persistent local skill storage."
                    ) from e

    async def register_skill(self, agent_id: str, skill: Skill) -> Skill:
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/skills/register", {
                "agent_id": agent_id,
                "skill_name": skill.name,
                "description": skill.description,
                "steps": skill.steps,
                "tools_required": skill.tools_required,
                "prerequisites": skill.prerequisites,
                "tags": skill.tags,
            })
            skill.version = resp.get("version", 1)
            skill.id = resp.get("id", skill.id)
            return skill

        key = f"{agent_id}:{skill.name}"
        if key in self._local_skills:
            existing = self._local_skills[key]
            skill.version = existing.version + 1
        self._local_skills[key] = skill

        if self._driver:
            await self._upsert_graph_node(agent_id, skill)
        elif self._pg_pool:
            await self._upsert_graph_node_pg(agent_id, skill)
        return skill

    async def record_execution(self, agent_id: str, skill_name: str,
                               success: bool, latency_ms: float):
        if self.api_client:
            await self.api_client._post("/memory-pipeline/skills/record-execution", {
                "agent_id": agent_id,
                "skill_name": skill_name,
                "success": success,
                "latency_ms": latency_ms,
            })
            return

        key = f"{agent_id}:{skill_name}"
        skill = self._local_skills.get(key)
        if skill:
            n = skill.execution_count
            skill.success_rate = (skill.success_rate * n + (1.0 if success else 0.0)) / (n + 1)
            skill.avg_latency_ms = (skill.avg_latency_ms * n + latency_ms) / (n + 1)
            skill.execution_count = n + 1

    async def find_skill(self, agent_id: str, query: str,
                         allowed_tags: list[str] | None = None,
                         allowed_domains: list[str] | None = None) -> list[Skill]:
        """``allowed_tags`` implements AgentConfig.scoped_subgraph ("Specialize")
        for skills. ``allowed_domains`` implements AgentConfig.scoped_domains
        -- Skill has no separate domains field, so a skill "belongs" to a
        kg_domains slug by carrying it in its own ``tags`` list alongside
        any other tags; the filter is the same set-intersection check,
        just against a different allowed-set. Both applied uniformly after
        fetch regardless of backend, same rationale as
        SemanticMemory._apply_scope."""
        if self.api_client:
            resp = await self.api_client._get(
                f"/memory-pipeline/skills/{agent_id}",
                params={"query": query},
            )
            results = resp.get("skills", [])
        else:
            q = query.lower()
            results = [
                s for s in await self._all_skills(agent_id)
                if q in s.name.lower() or q in s.description.lower()
            ]
        return self._apply_scope(self._apply_scope(results, allowed_domains), allowed_tags)

    async def list_skills(self, agent_id: str,
                          allowed_tags: list[str] | None = None,
                          allowed_domains: list[str] | None = None) -> list[Skill]:
        if self.api_client:
            resp = await self.api_client._get(f"/memory-pipeline/skills/{agent_id}")
            results = resp.get("skills", [])
        else:
            results = await self._all_skills(agent_id)
        return self._apply_scope(self._apply_scope(results, allowed_domains), allowed_tags)

    async def _all_skills(self, agent_id: str) -> list[Skill]:
        """The agent's skills: graph backend (durable) merged with the
        in-process dict (this session's not-yet-read-back writes).

        Previously reads NEVER touched the graph -- the Neo4j/AGE write in
        register_skill was fire-and-forget and find/list only consulted
        _local_skills, so every skill silently vanished on restart even
        with graph_db_url configured. In-process entries win on name
        collision (they are the freshest state, including this session's
        execution-count updates the graph may not have yet).
        """
        merged: dict[str, Skill] = {
            s.name: s for s in await self._load_skills_from_graph(agent_id)
        }
        for key, s in self._local_skills.items():
            if key.startswith(f"{agent_id}:"):
                merged[s.name] = s
        return list(merged.values())

    async def _load_skills_from_graph(self, agent_id: str) -> list[Skill]:
        if self._driver:
            async with self._driver.session() as session:
                result = await session.run(
                    "MATCH (s:Skill {agent_id: $agent_id}) RETURN s",
                    agent_id=agent_id,
                )
                records = await result.data()
            return [self._node_to_skill(r["s"]) for r in records]
        if self._pg_pool:
            async with self._pg_pool.acquire() as conn:
                try:
                    await conn.execute("LOAD 'age';")
                    await conn.execute("SET search_path = ag_catalog, '$user', public;")
                    rows = await conn.fetch(
                        """
                        SELECT * FROM cypher('vouchstone_graph', $$
                            MATCH (s:Skill {agent_id: %s}) RETURN properties(s)
                        $$) as (props agtype);
                        """,
                        agent_id,
                    )
                    return [
                        self._node_to_skill(json.loads(str(row["props"])))
                        for row in rows
                    ]
                except Exception as age_exc:
                    logger.debug(
                        "ProceduralMemory: AGE cypher read failed (%s); "
                        "falling back to kg_nodes SQL", age_exc,
                    )
                    rows = await conn.fetch(
                        "SELECT label, attributes FROM kg_nodes "
                        "WHERE tenant_id IS NULL AND kind = 'skill'"
                    )
                    out = []
                    for row in rows:
                        attrs = row["attributes"]
                        if isinstance(attrs, str):
                            attrs = json.loads(attrs)
                        out.append(self._node_to_skill({"name": row["label"], **(attrs or {})}))
                    return out
        return []

    @staticmethod
    def _node_to_skill(props: dict[str, Any]) -> Skill:
        def _as_list(value: Any) -> list[str]:
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    return parsed if isinstance(parsed, list) else []
                except (json.JSONDecodeError, ValueError):
                    return []
            return list(value) if isinstance(value, list) else []

        name = str(props.get("name", ""))
        return Skill(
            id=hashlib.md5(name.encode()).hexdigest(),
            name=name,
            description=str(props.get("description", name)),
            steps=_as_list(props.get("steps")),
            tools_required=_as_list(props.get("tools_required")),
            version=int(props.get("version", 1) or 1),
            success_rate=float(props.get("success_rate", 0.0) or 0.0),
            execution_count=int(props.get("execution_count", 0) or 0),
        )

    @staticmethod
    def _apply_scope(skills: list[Skill], allowed_tags: list[str] | None) -> list[Skill]:
        if not allowed_tags:
            return skills
        allowed = set(allowed_tags)
        return [s for s in skills if allowed.intersection(getattr(s, "tags", None) or [])]

    async def _upsert_graph_node(self, agent_id: str, skill: Skill):
        if not self._driver:
            return
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (s:Skill {agent_id: $agent_id, name: $name})
                SET s.description = $description,
                    s.steps = $steps,
                    s.tools_required = $tools,
                    s.version = $version,
                    s.success_rate = $success_rate,
                    s.execution_count = $exec_count
                """,
                agent_id=agent_id, name=skill.name,
                description=skill.description,
                steps=json.dumps(skill.steps),
                tools=json.dumps(skill.tools_required),
                version=skill.version,
                exec_count=skill.execution_count,
            )
            for prereq in skill.prerequisites:
                await session.run(
                    """
                    MERGE (s:Skill {agent_id: $agent_id, name: $name})
                    MERGE (p:Skill {agent_id: $agent_id, name: $prereq})
                    MERGE (s)-[:REQUIRES]->(p)
                    """,
                    agent_id=agent_id, name=skill.name, prereq=prereq,
                )

    async def _upsert_graph_node_pg(self, agent_id: str, skill: Skill):
        if not self._pg_pool:
            return
        from uuid import uuid4
        async with self._pg_pool.acquire() as conn:
            try:
                await conn.execute("LOAD 'age';")
                await conn.execute("SET search_path = ag_catalog, '$user', public;")
                await conn.execute("SELECT create_graph('vouchstone_graph') WHERE NOT EXISTS (SELECT 1 FROM ag_graph WHERE name = 'vouchstone_graph');")
                
                query = """
                    SELECT * FROM cypher('vouchstone_graph', $$
                        MERGE (s:Skill {agent_id: %s, name: %s})
                        SET s.description = %s,
                            s.steps = %s,
                            s.tools_required = %s,
                            s.version = %s,
                            s.success_rate = %s,
                            s.execution_count = %s
                    $$) as (a agtype);
                """
                await conn.execute(
                    query,
                    agent_id, skill.name, skill.description,
                    json.dumps(skill.steps), json.dumps(skill.tools_required),
                    skill.version, skill.success_rate, skill.execution_count
                )
                
                for prereq in skill.prerequisites:
                    prereq_query = """
                        SELECT * FROM cypher('vouchstone_graph', $$
                            MERGE (s:Skill {agent_id: %s, name: %s})
                            MERGE (p:Skill {agent_id: %s, name: %s})
                            MERGE (s)-[:REQUIRES]->(p)
                        $$) as (a agtype);
                    """
                    await conn.execute(prereq_query, agent_id, skill.name, agent_id, prereq)
                return
            except Exception as age_exc:
                # Intentional fallback: a Postgres without the AGE extension
                # still persists skills via the plain kg_nodes table below.
                logger.debug(
                    "ProceduralMemory: AGE cypher path failed for skill %r "
                    "(%s); falling back to kg_nodes SQL", skill.name, age_exc,
                )
                try:
                    attrs = {
                        "steps": skill.steps,
                        "tools_required": skill.tools_required,
                        "version": skill.version,
                        "success_rate": skill.success_rate,
                        "execution_count": skill.execution_count,
                    }
                    node_id = str(uuid4())
                    
                    sql_check = "SELECT id FROM kg_nodes WHERE tenant_id IS NULL AND kind = 'skill' AND label = $1 LIMIT 1"
                    existing_id = await conn.fetchval(sql_check, skill.name)
                    if existing_id:
                        sql_update = """
                            UPDATE kg_nodes 
                            SET attributes = $1, confidence = $2 
                            WHERE id = $3
                        """
                        await conn.execute(sql_update, json.dumps(attrs), 1.0, existing_id)
                    else:
                        sql_insert = """
                            INSERT INTO kg_nodes (id, kind, label, attributes, confidence, status, regulator_tags)
                            VALUES ($1, 'skill', $2, $3, 1.0, 'promoted', $4)
                        """
                        await conn.execute(sql_insert, node_id, skill.name, json.dumps(attrs), json.dumps([]))
                except Exception as sql_exc:
                    # Both graph persistence paths failed: the skill exists
                    # only in this process's memory and will NOT survive a
                    # restart despite graph_db_url being configured. That is
                    # a durability failure the operator must see -- this was
                    # previously a bare `pass`, silently violating the
                    # explicit-backend-means-durability contract that
                    # MemoryBackendUnavailableError enforces at initialize().
                    self.graph_write_errors += 1
                    logger.error(
                        "ProceduralMemory: graph persistence FAILED for skill "
                        "%r (agent %s) -- AGE cypher and kg_nodes SQL both "
                        "errored; the skill is in-process only and will not "
                        "survive a restart. Last error: %s",
                        skill.name, agent_id, sql_exc,
                    )

    # Legacy compatibility
    async def store_procedure(self, name: str, steps: list[str],
                               conditions: dict[str, Any] | None = None) -> str:
        skill = Skill(
            id=hashlib.md5(name.encode()).hexdigest(),
            name=name, description=name, steps=steps,
        )
        self._local_skills[f"legacy:{name}"] = skill
        return name

    async def get_relevant(self, context: str) -> list[dict[str, Any]]:
        context_lower = context.lower()
        return [
            {"name": s.name, "steps": s.steps, "success_rate": s.success_rate}
            for s in self._local_skills.values()
            if s.name.lower() in context_lower
        ]

    async def close(self):
        if self._driver:
            await self._driver.close()
        if self._pg_pool:
            await self._pg_pool.close()


class MetaMemory:
    """Layer 5 — Memory governance running on the control plane.

    Manages decay, deduplication, compression, archival, and forgetting
    across all other memory layers.
    """

    def __init__(self, api_client=None):
        self.api_client = api_client

    # Meta-memory (decay/dedup/compression/reflection) runs ONLY on the
    # control plane -- there is no local implementation. Offline returns
    # carry an explicit "unavailable" status rather than success-shaped
    # empties: a caller inspecting {"operations": []} previously could not
    # distinguish "maintenance ran and found nothing to do" from
    # "maintenance never ran at all".
    _OFFLINE_REASON = (
        "meta-memory runs on the control plane; no api_client is configured"
    )

    async def run_maintenance(self, agent_id: str) -> dict[str, Any]:
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/maintenance", {
                "agent_id": agent_id,
            })
            return resp
        return {"status": "unavailable", "reason": self._OFFLINE_REASON, "operations": []}

    async def get_health(self, agent_id: str) -> HealthReport:
        if self.api_client:
            resp = await self.api_client._get(f"/memory-pipeline/health/{agent_id}")
            return HealthReport(
                total_entries=resp.get("total_entries", 0),
                per_layer=resp.get("per_layer", {}),
                recommendations=resp.get("recommendations", []),
            )
        return HealthReport(recommendations=[f"unavailable: {self._OFFLINE_REASON}"])

    async def run_reflection(self, agent_id: str, session_id: str) -> dict[str, Any]:
        if self.api_client:
            return await self.api_client._post(f"/memory-pipeline/reflect/{agent_id}", {
                "session_id": session_id,
            })
        return {"status": "unavailable", "reason": self._OFFLINE_REASON, "skills_discovered": 0}


class MemoryPipeline:
    """Orchestrates the 5-layer memory stack for an agent.

    Usage:
        pipeline = MemoryPipeline(agent_id="...", redis_url="...", ...)
        await pipeline.initialize()

        # Before each turn
        context = await pipeline.prepare_context(session_id, user_input)

        # After each turn
        result = await pipeline.process_turn(session_id, turn_number, ...)

        # Periodic
        await pipeline.run_reflection(session_id)
        await pipeline.run_maintenance()
    """

    def __init__(
        self,
        agent_id: str,
        redis_url: str | None = None,
        vector_db_url: str | None = None,
        graph_db_url: str | None = None,
        api_client=None,
        scoped_subgraph: list[str] | None = None,
        scoped_domains: list[str] | None = None,
        local_only: bool = False,
        enabled_layers: dict[str, bool] | None = None,
        embedding_model: str | None = None,
        retention_days: int | None = None,
    ):
        self.agent_id = agent_id
        # See AgentConfig.scoped_subgraph -- constrains prepare_context()'s
        # semantic/procedural queries to this list of entity types / skill
        # tags. None means unscoped (matches pre-scoping behavior exactly).
        self.scoped_subgraph = scoped_subgraph
        # See AgentConfig.scoped_domains -- a second, independent kg_domains
        # slug filter (flat match, see agent.py's field docstring for why
        # there's no hierarchy expansion here).
        self.scoped_domains = scoped_domains
        # Per-layer enable/disable, keyed "working" / "episodic" /
        # "semantic" / "procedural" / "meta". Backs AgentConfig's five
        # layer booleans -- which were documented but never read anywhere
        # before this parameter existed. A disabled layer contributes its
        # empty default to MemoryContext and skips its writes; its backend
        # is never initialized.
        layers = enabled_layers or {}
        self.layer_enabled: dict[str, bool] = {
            name: bool(layers.get(name, True))
            for name in ("working", "episodic", "semantic", "procedural", "meta")
        }
        self.working = WorkingMemory(redis_url=redis_url)
        self.episodic = EpisodicMemory(
            api_client=api_client,
            **({"retention_days": retention_days} if retention_days is not None else {}),
        )
        self.semantic = SemanticMemory(
            vector_db_url=vector_db_url, api_client=api_client, local_only=local_only,
            **({"embedding_model": embedding_model} if embedding_model else {}),
        )
        self.procedural = ProceduralMemory(
            graph_db_url=graph_db_url, api_client=api_client
        )
        self.meta = MetaMemory(api_client=api_client)

    async def initialize(self):
        tasks = []
        if self.layer_enabled["working"]:
            tasks.append(self.working.initialize())
        if self.layer_enabled["episodic"]:
            tasks.append(self.episodic.initialize())
        if self.layer_enabled["semantic"]:
            tasks.append(self.semantic.initialize())
        if self.layer_enabled["procedural"]:
            tasks.append(self.procedural.initialize())
        if tasks:
            await asyncio.gather(*tasks)

    @staticmethod
    async def _empty_list() -> list[Any]:
        return []

    @staticmethod
    async def _empty_dict() -> dict[str, Any]:
        return {}

    async def prepare_context(self, session_id: str, user_input: str,
                              max_tokens: int = 4000) -> MemoryContext:
        wm_task = (
            self.working.get_context(self.agent_id, session_id, max_tokens)
            if self.layer_enabled["working"] else self._empty_list()
        )
        ep_task = (
            self.episodic.get_recent(self.agent_id, session_id)
            if self.layer_enabled["episodic"] else self._empty_list()
        )
        sem_task = (
            self.semantic.search_entities(
                self.agent_id, user_input,
                allowed_types=self.scoped_subgraph, allowed_domains=self.scoped_domains,
            )
            if self.layer_enabled["semantic"] else self._empty_list()
        )
        proc_task = (
            self.procedural.find_skill(
                self.agent_id, user_input,
                allowed_tags=self.scoped_subgraph, allowed_domains=self.scoped_domains,
            )
            if self.layer_enabled["procedural"] else self._empty_list()
        )
        scratch_task = (
            self.working.get_scratchpad(self.agent_id, session_id)
            if self.layer_enabled["working"] else self._empty_dict()
        )

        wm, ep, sem, proc, scratch = await asyncio.gather(
            wm_task, ep_task, sem_task, proc_task, scratch_task
        )

        if self.layer_enabled["working"]:
            await self.working.append(self.agent_id, session_id, "user", user_input)

        return MemoryContext(
            working_memory=wm,
            episodic_context=ep,
            semantic_entities=sem,
            procedural_skills=proc,
            scratchpad=scratch,
        )

    async def process_turn(
        self,
        session_id: str,
        turn_number: int,
        user_input: str,
        agent_response: str,
        tools_used: list[str] | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        success: bool = True,
    ) -> TurnResult:
        if self.layer_enabled["working"]:
            await self.working.append(
                self.agent_id, session_id, "assistant", agent_response
            )

        if not self.layer_enabled["episodic"]:
            return TurnResult(episodic_trace_id="")

        trace = EpisodicTrace(
            id=f"trace_{self.agent_id}_{session_id}_{turn_number}",
            session_id=session_id,
            turn_number=turn_number,
            user_input=user_input,
            agent_response=agent_response,
            tools_used=tools_used or [],
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            success=success,
        )
        trace_id = await self.episodic.append_trace(self.agent_id, trace)

        return TurnResult(episodic_trace_id=trace_id)

    async def run_reflection(self, session_id: str) -> dict[str, Any]:
        if not self.layer_enabled["meta"]:
            return {"status": "disabled", "reason": "meta layer disabled in AgentConfig",
                    "skills_discovered": 0}
        return await self.meta.run_reflection(self.agent_id, session_id)

    async def run_maintenance(self) -> dict[str, Any]:
        if not self.layer_enabled["meta"]:
            return {"status": "disabled", "reason": "meta layer disabled in AgentConfig",
                    "operations": []}
        return await self.meta.run_maintenance(self.agent_id)

    def health_detail(self) -> dict[str, Any]:
        """Surfaces degraded-mode state that doesn't raise on init (only
        SemanticMemory can end up here today -- WorkingMemory and
        ProceduralMemory raise MemoryBackendUnavailableError instead when an
        explicitly configured backend is unreachable, so a caller who wants
        that failure surfaced needs to catch it around pipeline.initialize()
        rather than poll this)."""
        return {
            "semantic_degraded": self.semantic.degraded,
            "semantic_degraded_reason": self.semantic.degraded_reason,
        }

    async def get_snapshot(self) -> dict[str, Any]:
        if self.meta.api_client:
            return await self.meta.api_client._get(
                f"/memory-pipeline/snapshot/{self.agent_id}"
            )
        return {
            "episodic": len(self.episodic._local_traces),
            "semantic": len(self.semantic._local_entities),
            "procedural": len(self.procedural._local_skills),
        }

    async def end_session(self, session_id: str):
        await self.working.clear_session(self.agent_id, session_id)

    async def close(self):
        await asyncio.gather(
            self.working.close(),
            self.episodic.close(),
            self.semantic.close(),
            self.procedural.close(),
        )
