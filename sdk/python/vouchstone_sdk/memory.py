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

from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import hashlib
import asyncio
import logging

from .types import (
    MemoryEntry, Decision, Episode, EpisodicTrace, Entity,
    Skill, MemoryContext, TurnResult, HealthReport, MemoryLayer,
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

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url
        self._redis = None
        self._local: Dict[str, List[Dict[str, Any]]] = {}
        self._scratchpads: Dict[str, Dict[str, Any]] = {}

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
                     metadata: Dict[str, Any] = None):
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
                          max_tokens: int = 4000) -> List[Dict[str, Any]]:
        if self._redis:
            key = self._key(agent_id, session_id)
            raw = await self._redis.lrange(key, 0, -1)
            entries = [json.loads(r) for r in raw]
        else:
            key = self._key(agent_id, session_id)
            entries = list(self._local.get(key, []))

        result = []
        budget = max_tokens
        for entry in reversed(entries):
            cost = len(entry["content"]) // 4
            if cost > budget:
                break
            result.insert(0, entry)
            budget -= cost
        return result

    async def set_scratchpad(self, agent_id: str, session_id: str,
                             data: Dict[str, Any]):
        if self._redis:
            key = f"scratch:{agent_id}:{session_id}"
            await self._redis.set(key, json.dumps(data), ex=self.SESSION_TTL)
        else:
            self._scratchpads[f"{agent_id}:{session_id}"] = data

    async def get_scratchpad(self, agent_id: str, session_id: str) -> Dict[str, Any]:
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
        self._local_traces: List[EpisodicTrace] = []

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

    async def get_recent(self, agent_id: str, session_id: Optional[str] = None,
                         limit: int = 20) -> List[EpisodicTrace]:
        if self.api_client:
            params = {"limit": limit}
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

    async def search(self, query: str, limit: int = 5) -> List[EpisodicTrace]:
        q = query.lower()
        matches = [
            t for t in self._local_traces
            if q in t.user_input.lower() or q in t.agent_response.lower()
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
        vector_db_url: Optional[str] = None,
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
        self._chroma_client = None
        self._embedder = None
        self._local_entities: Dict[str, Entity] = {}
        # Set when no vector_db_url was configured and the local embedded
        # Chroma client also couldn't be started -- search_entities() falls
        # back to plain substring matching with no vector search at all.
        # Unlike a configured backend being unreachable, this doesn't raise
        # (no explicit durability promise was made), but it must be visible
        # to callers, not silent -- see MemoryPipeline.health_detail().
        self.degraded = False
        self.degraded_reason: Optional[str] = None

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
                              allowed_types: Optional[List[str]] = None) -> List[Entity]:
        """``allowed_types`` implements AgentConfig.scoped_subgraph ("Specialize")
        -- when set, results are filtered to only those entity types, applied
        uniformly after fetch regardless of which backend served the query
        (api_client, vector search, or local dict), so scoping can't be
        silently bypassed by whichever backend happens to be active."""
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

        return self._apply_scope(results, allowed_types)

    async def list_entities(self, agent_id: str,
                            entity_type: Optional[str] = None,
                            allowed_types: Optional[List[str]] = None) -> List[Entity]:
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

        return self._apply_scope(results, allowed_types)

    @staticmethod
    def _apply_scope(entities: List[Entity], allowed_types: Optional[List[str]]) -> List[Entity]:
        if not allowed_types:
            return entities
        allowed = set(allowed_types)
        return [e for e in entities if getattr(e, "entity_type", None) in allowed]

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
                             top_k: int) -> List[Entity]:
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

    async def _get_embedding(self, text: str) -> List[float]:
        """Raises on failure -- a fake zero-vector would silently corrupt
        every downstream cosine-similarity search (a zero-vector isn't "no
        opinion", it produces meaningless/degenerate similarity scores that
        look like real results). Callers must handle the failure, not
        receive corrupted data that looks valid."""
        import openai
        client = openai.AsyncOpenAI()
        response = await client.embeddings.create(
            model=self.embedding_model, input=text
        )
        return response.data[0].embedding

    # Legacy compatibility
    async def store(self, text: str, metadata: Dict[str, Any] = None) -> str:
        doc_id = hashlib.md5(text.encode()).hexdigest()
        if self._chroma_client:
            embedding = await self._get_embedding(text)
            collection = self._chroma_client.get_or_create_collection(self.collection_name)
            collection.add(
                ids=[doc_id], embeddings=[embedding],
                documents=[text], metadatas=[metadata or {}],
            )
        return doc_id

    async def search(self, query: str, top_k: int = 5) -> List[MemoryEntry]:
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

    def __init__(self, graph_db_url: Optional[str] = None, api_client=None):
        self.graph_db_url = graph_db_url
        self.api_client = api_client
        self._driver = None
        self._pg_pool = None
        self._local_skills: Dict[str, Skill] = {}

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
                         allowed_tags: Optional[List[str]] = None) -> List[Skill]:
        """``allowed_tags`` implements AgentConfig.scoped_subgraph ("Specialize")
        for skills -- applied uniformly after fetch regardless of backend,
        same rationale as SemanticMemory._apply_scope."""
        if self.api_client:
            resp = await self.api_client._get(
                f"/memory-pipeline/skills/{agent_id}",
                params={"query": query},
            )
            results = resp.get("skills", [])
        else:
            q = query.lower()
            results = [
                s for key, s in self._local_skills.items()
                if key.startswith(f"{agent_id}:") and (
                    q in s.name.lower() or q in s.description.lower()
                )
            ]
        return self._apply_scope(results, allowed_tags)

    async def list_skills(self, agent_id: str,
                          allowed_tags: Optional[List[str]] = None) -> List[Skill]:
        if self.api_client:
            resp = await self.api_client._get(f"/memory-pipeline/skills/{agent_id}")
            results = resp.get("skills", [])
        else:
            results = [
                s for key, s in self._local_skills.items()
                if key.startswith(f"{agent_id}:")
            ]
        return self._apply_scope(results, allowed_tags)

    @staticmethod
    def _apply_scope(skills: List[Skill], allowed_tags: Optional[List[str]]) -> List[Skill]:
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
            except Exception:
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
                except Exception:
                    pass

    # Legacy compatibility
    async def store_procedure(self, name: str, steps: List[str],
                               conditions: Dict[str, Any] = None) -> str:
        skill = Skill(
            id=hashlib.md5(name.encode()).hexdigest(),
            name=name, description=name, steps=steps,
        )
        self._local_skills[f"legacy:{name}"] = skill
        return name

    async def get_relevant(self, context: str) -> List[Dict[str, Any]]:
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

    async def run_maintenance(self, agent_id: str) -> Dict[str, Any]:
        if self.api_client:
            resp = await self.api_client._post("/memory-pipeline/maintenance", {
                "agent_id": agent_id,
            })
            return resp
        return {"status": "no_api_client", "operations": []}

    async def get_health(self, agent_id: str) -> HealthReport:
        if self.api_client:
            resp = await self.api_client._get(f"/memory-pipeline/health/{agent_id}")
            return HealthReport(
                total_entries=resp.get("total_entries", 0),
                per_layer=resp.get("per_layer", {}),
                recommendations=resp.get("recommendations", []),
            )
        return HealthReport()

    async def run_reflection(self, agent_id: str, session_id: str) -> Dict[str, Any]:
        if self.api_client:
            return await self.api_client._post(f"/memory-pipeline/reflect/{agent_id}", {
                "session_id": session_id,
            })
        return {"skills_discovered": 0}


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
        redis_url: Optional[str] = None,
        vector_db_url: Optional[str] = None,
        graph_db_url: Optional[str] = None,
        api_client=None,
        scoped_subgraph: Optional[List[str]] = None,
        local_only: bool = False,
    ):
        self.agent_id = agent_id
        # See AgentConfig.scoped_subgraph -- constrains prepare_context()'s
        # semantic/procedural queries to this list of entity types / skill
        # tags. None means unscoped (matches pre-scoping behavior exactly).
        self.scoped_subgraph = scoped_subgraph
        self.working = WorkingMemory(redis_url=redis_url)
        self.episodic = EpisodicMemory(api_client=api_client)
        self.semantic = SemanticMemory(
            vector_db_url=vector_db_url, api_client=api_client, local_only=local_only,
        )
        self.procedural = ProceduralMemory(
            graph_db_url=graph_db_url, api_client=api_client
        )
        self.meta = MetaMemory(api_client=api_client)

    async def initialize(self):
        await asyncio.gather(
            self.working.initialize(),
            self.episodic.initialize(),
            self.semantic.initialize(),
            self.procedural.initialize(),
        )

    async def prepare_context(self, session_id: str, user_input: str,
                              max_tokens: int = 4000) -> MemoryContext:
        wm_task = self.working.get_context(self.agent_id, session_id, max_tokens)
        ep_task = self.episodic.get_recent(self.agent_id, session_id)
        sem_task = self.semantic.search_entities(
            self.agent_id, user_input, allowed_types=self.scoped_subgraph
        )
        proc_task = self.procedural.find_skill(
            self.agent_id, user_input, allowed_tags=self.scoped_subgraph
        )
        scratch_task = self.working.get_scratchpad(self.agent_id, session_id)

        wm, ep, sem, proc, scratch = await asyncio.gather(
            wm_task, ep_task, sem_task, proc_task, scratch_task
        )

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
        tools_used: List[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        success: bool = True,
    ) -> TurnResult:
        await self.working.append(
            self.agent_id, session_id, "assistant", agent_response
        )

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

    async def run_reflection(self, session_id: str) -> Dict[str, Any]:
        return await self.meta.run_reflection(self.agent_id, session_id)

    async def run_maintenance(self) -> Dict[str, Any]:
        return await self.meta.run_maintenance(self.agent_id)

    def health_detail(self) -> Dict[str, Any]:
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

    async def get_snapshot(self) -> Dict[str, Any]:
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
