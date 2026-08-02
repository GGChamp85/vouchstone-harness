"""Base ingester for Knowledge Graph ingestion pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
import openai

logger = logging.getLogger("vouchstone.ingestion")


class EntityType(str, Enum):
    DECISION = "decision"
    ACTION_ITEM = "action_item"
    TASK = "task"
    BLOCKER = "blocker"
    KNOWLEDGE = "knowledge"
    PERSON = "person"
    SYSTEM = "system"
    PROCESS = "process"
    CODE_CHANGE = "code_change"
    ARCHITECTURE = "architecture"
    RISK = "risk"
    QUESTION = "question"
    TOPIC = "topic"
    DEPENDENCY = "dependency"
    DOCUMENT = "document"
    MEETING = "meeting"


class SyncState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    PAUSED = "paused"


@dataclass
class RawEvent:
    """A raw event fetched from an external source."""
    source: str
    type: str
    content: str
    timestamp: datetime
    author: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            h = hashlib.sha256(
                f"{self.source}:{self.type}:{self.timestamp.isoformat()}:{self.content[:200]}".encode()
            ).hexdigest()[:16]
            self.id = f"evt-{h}"


@dataclass
class Entity:
    """An entity extracted from raw events for the Knowledge Graph."""
    id: str
    type: EntityType
    name: str
    description: str
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    source_event_id: str = ""
    confidence: float = 1.0
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def dedup_key(self) -> str:
        return f"{self.type.value}:{self.name.lower().strip()}"


@dataclass
class Relationship:
    """A relationship between two entities in the Knowledge Graph."""
    source_entity_id: str
    target_entity_id: str
    type: str
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def dedup_key(self) -> str:
        return f"{self.source_entity_id}:{self.type}:{self.target_entity_id}"


@dataclass
class SyncStatus:
    """Status of an ingestion sync operation."""
    source: str
    last_sync: datetime | None = None
    entities_ingested: int = 0
    relationships_created: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)
    status: SyncState = SyncState.IDLE
    duration_seconds: float = 0.0


class BaseIngester(ABC):
    """Abstract base for all KG ingesters.

    Subclasses implement source-specific fetch and extraction logic.
    The base class handles LLM-powered entity extraction, KG writes,
    and sync orchestration.
    """

    source_name: str = "unknown"

    def __init__(
        self,
        *,
        chromadb_url: str = "http://localhost:8000",
        neo4j_url: str = "bolt://localhost:7687",
        neo4j_user: str = "neo4j",
        neo4j_password: str = "password",
        openai_api_key: str | None = None,
        openai_model: str = "gpt-4o-mini",
        collection_name: str = "vouchstone_kg",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._chromadb_url = chromadb_url
        self._neo4j_url = neo4j_url
        self._neo4j_user = neo4j_user
        self._neo4j_password = neo4j_password
        self._collection = collection_name
        self._openai_model = openai_model
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http_client is None
        self._sync_status = SyncStatus(source=self.source_name)

        if openai_api_key:
            self._oai = openai.AsyncOpenAI(api_key=openai_api_key)
        else:
            self._oai = openai.AsyncOpenAI()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the external source (OAuth, API key, etc.)."""

    @abstractmethod
    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        """Fetch raw events from the source since the given timestamp."""

    async def extract_entities(self, events: list[RawEvent]) -> list[Entity]:
        """Use LLM to extract structured entities from raw events."""
        if not events:
            return []

        batch_size = 20
        all_entities: list[Entity] = []

        for i in range(0, len(events), batch_size):
            batch = events[i : i + batch_size]
            events_text = "\n\n".join(
                f"[{e.timestamp.isoformat()}] ({e.type}) by {e.author}: {e.content[:500]}"
                for e in batch
            )

            try:
                resp = await self._oai.chat.completions.create(
                    model=self._openai_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are an entity extraction engine for an enterprise knowledge graph. "
                                "Extract structured entities from the events below. For each entity, provide:\n"
                                '- type: one of [decision, action_item, task, blocker, knowledge, person, '
                                'system, process, code_change, architecture, risk, question, topic, dependency, document, meeting]\n'
                                "- name: short descriptive name\n"
                                "- description: one-sentence description\n"
                                "- attributes: relevant key-value pairs\n"
                                "- confidence: 0.0 to 1.0\n\n"
                                "Return a JSON array of entities. Be precise and avoid duplicates."
                            ),
                        },
                        {"role": "user", "content": events_text},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                )

                content = resp.choices[0].message.content or "{}"
                parsed = json.loads(content)
                entities_data = parsed.get("entities", [])

                for ed in entities_data:
                    etype = ed.get("type", "knowledge")
                    try:
                        entity_type = EntityType(etype)
                    except ValueError:
                        entity_type = EntityType.KNOWLEDGE

                    name = ed.get("name", "Unknown")
                    eid = hashlib.sha256(
                        f"{entity_type.value}:{name}".encode()
                    ).hexdigest()[:16]

                    all_entities.append(
                        Entity(
                            id=f"ent-{eid}",
                            type=entity_type,
                            name=name,
                            description=ed.get("description", ""),
                            attributes=ed.get("attributes", {}),
                            source=self.source_name,
                            confidence=float(ed.get("confidence", 0.8)),
                        )
                    )

            except Exception as exc:
                logger.error("Entity extraction failed for batch %d: %s", i, exc)
                self._sync_status.errors += 1
                self._sync_status.error_messages.append(str(exc))

        return all_entities

    async def extract_relationships(self, entities: list[Entity]) -> list[Relationship]:
        """Use LLM to extract relationships between entities."""
        if len(entities) < 2:
            return []

        entity_list = "\n".join(
            f"- {e.id}: [{e.type.value}] {e.name} — {e.description[:100]}"
            for e in entities[:50]
        )

        try:
            resp = await self._oai.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a relationship extraction engine for an enterprise knowledge graph. "
                            "Given the entities below, identify relationships between them.\n"
                            "For each relationship provide:\n"
                            "- source_id: the source entity ID\n"
                            "- target_id: the target entity ID\n"
                            "- type: relationship type (e.g., depends_on, blocks, assigned_to, "
                            "part_of, decided_in, modifies, approves, creates, references)\n"
                            "- confidence: 0.0 to 1.0\n\n"
                            "Return a JSON object with a 'relationships' array."
                        ),
                    },
                    {"role": "user", "content": entity_list},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )

            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            rels_data = parsed.get("relationships", [])
            entity_ids = {e.id for e in entities}

            relationships = []
            for rd in rels_data:
                src = rd.get("source_id", "")
                tgt = rd.get("target_id", "")
                if src in entity_ids and tgt in entity_ids:
                    relationships.append(
                        Relationship(
                            source_entity_id=src,
                            target_entity_id=tgt,
                            type=rd.get("type", "related_to"),
                            confidence=float(rd.get("confidence", 0.7)),
                            metadata=rd.get("metadata", {}),
                        )
                    )

            return relationships

        except Exception as exc:
            logger.error("Relationship extraction failed: %s", exc)
            self._sync_status.errors += 1
            self._sync_status.error_messages.append(str(exc))
            return []

    async def _generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        resp = await self._oai.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],
        )
        return resp.data[0].embedding

    async def ingest(
        self, entities: list[Entity], relationships: list[Relationship]
    ) -> None:
        """Write entities and relationships to the Knowledge Graph."""
        # Write to ChromaDB (vector store)
        if entities:
            embeddings_tasks = [
                self._generate_embedding(f"{e.name}: {e.description}")
                for e in entities
            ]
            embeddings = await asyncio.gather(*embeddings_tasks, return_exceptions=True)

            ids = []
            documents = []
            metadatas = []
            valid_embeddings = []

            for e, emb in zip(entities, embeddings):
                if isinstance(emb, Exception):
                    logger.warning("Embedding failed for %s: %s", e.id, emb)
                    continue
                ids.append(e.id)
                documents.append(f"{e.name}: {e.description}")
                metadatas.append(
                    {
                        "type": e.type.value,
                        "name": e.name,
                        "source": e.source,
                        "confidence": e.confidence,
                        "created_at": e.created_at.isoformat(),
                        **{k: str(v) for k, v in e.attributes.items()},
                    }
                )
                valid_embeddings.append(emb)

            if ids:
                try:
                    await self._http.post(
                        f"{self._chromadb_url}/api/v1/collections/{self._collection}/upsert",
                        json={
                            "ids": ids,
                            "documents": documents,
                            "metadatas": metadatas,
                            "embeddings": valid_embeddings,
                        },
                    )
                    logger.info("Upserted %d entities to ChromaDB", len(ids))
                except Exception as exc:
                    logger.error("ChromaDB upsert failed: %s", exc)
                    self._sync_status.errors += 1
                    self._sync_status.error_messages.append(f"ChromaDB: {exc}")

        # Write to Neo4j (graph store)
        if entities or relationships:
            try:
                import neo4j as neo4j_driver

                driver = neo4j_driver.AsyncGraphDatabase.driver(
                    self._neo4j_url,
                    auth=(self._neo4j_user, self._neo4j_password),
                )
                async with driver.session() as session:
                    for e in entities:
                        await session.run(
                            "MERGE (n:Entity {id: $id}) "
                            "SET n.type = $type, n.name = $name, "
                            "n.description = $description, n.source = $source, "
                            "n.confidence = $confidence, n.updated_at = datetime()",
                            id=e.id,
                            type=e.type.value,
                            name=e.name,
                            description=e.description,
                            source=e.source,
                            confidence=e.confidence,
                        )

                    for r in relationships:
                        await session.run(
                            "MATCH (a:Entity {id: $src}), (b:Entity {id: $tgt}) "
                            "MERGE (a)-[rel:RELATES {type: $type}]->(b) "
                            "SET rel.confidence = $confidence, "
                            "rel.updated_at = datetime()",
                            src=r.source_entity_id,
                            tgt=r.target_entity_id,
                            type=r.type,
                            confidence=r.confidence,
                        )

                    logger.info(
                        "Wrote %d entities and %d relationships to Neo4j",
                        len(entities),
                        len(relationships),
                    )

                await driver.close()

            except Exception as exc:
                logger.error("Neo4j write failed: %s", exc)
                self._sync_status.errors += 1
                self._sync_status.error_messages.append(f"Neo4j: {exc}")

    async def sync(self, since: datetime) -> SyncStatus:
        """Full sync pipeline: fetch → extract entities → extract relationships → ingest."""
        start = datetime.now(timezone.utc)
        self._sync_status = SyncStatus(
            source=self.source_name, status=SyncState.RUNNING
        )

        try:
            raw_events = await self.fetch_raw(since)
            logger.info("[%s] Fetched %d raw events", self.source_name, len(raw_events))

            entities = await self.extract_entities(raw_events)
            logger.info("[%s] Extracted %d entities", self.source_name, len(entities))

            relationships = await self.extract_relationships(entities)
            logger.info(
                "[%s] Extracted %d relationships", self.source_name, len(relationships)
            )

            await self.ingest(entities, relationships)

            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            self._sync_status.last_sync = datetime.now(timezone.utc)
            self._sync_status.entities_ingested = len(entities)
            self._sync_status.relationships_created = len(relationships)
            self._sync_status.duration_seconds = elapsed
            self._sync_status.status = (
                SyncState.SUCCESS if self._sync_status.errors == 0 else SyncState.ERROR
            )

        except Exception as exc:
            logger.exception("[%s] Sync failed: %s", self.source_name, exc)
            self._sync_status.status = SyncState.ERROR
            self._sync_status.errors += 1
            self._sync_status.error_messages.append(str(exc))
            self._sync_status.duration_seconds = (
                datetime.now(timezone.utc) - start
            ).total_seconds()

        return self._sync_status

    def get_sync_status(self) -> SyncStatus:
        return self._sync_status
