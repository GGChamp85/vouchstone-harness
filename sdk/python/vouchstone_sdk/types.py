"""Type definitions for Vouchstone SDK"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass
class Message:
    """Input message to an agent"""
    content: str
    role: str = "user"
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Decision:
    """A decision made by an agent"""
    id: str
    question: str
    answer: str
    confidence: float
    reasoning: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AgentResponse:
    """Response from an agent"""
    content: str
    decisions: list[Decision] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class MemoryEntry:
    """An entry from memory search"""
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Episode:
    """An episodic memory entry"""
    id: str
    timestamp: datetime
    message: str
    response: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodicTrace:
    """A trace from the 5-layer episodic memory"""
    id: str
    session_id: str
    turn_number: int
    user_input: str
    agent_response: str
    tools_used: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    success: bool = True
    importance: float = 0.5
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Entity:
    """A semantic entity in the knowledge graph"""
    id: str
    entity_type: str
    entity_key: str
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    source_trace_id: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Skill:
    """A procedural skill in the memory graph"""
    id: str
    name: str
    description: str
    steps: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    execution_count: int = 0
    version: int = 1
    # Domain tags (e.g. "finance", "compliance", "migration") -- used by
    # AgentConfig.scoped_subgraph to constrain which skills an agent's
    # find_skill()/list_skills() calls can surface. Empty by default
    # (unscoped/matches nothing specific; only visible to unscoped agents).
    tags: list[str] = field(default_factory=list)


@dataclass
class MemoryContext:
    """Full context prepared by the memory pipeline before a turn"""
    working_memory: list[dict[str, Any]] = field(default_factory=list)
    episodic_context: list[EpisodicTrace] = field(default_factory=list)
    semantic_entities: list[Entity] = field(default_factory=list)
    procedural_skills: list[Skill] = field(default_factory=list)
    scratchpad: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    """Result of processing a turn through the memory pipeline"""
    episodic_trace_id: str
    entities_extracted: int = 0
    skills_updated: int = 0


@dataclass
class HealthReport:
    """Memory health report from meta-memory"""
    total_entries: int = 0
    per_layer: dict[str, int] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    last_maintenance: datetime | None = None


@dataclass
class AgentDefinition:
    """Agent definition from control plane"""
    id: str
    name: str
    slug: str
    agent_type: str
    status: str
    memory_config: dict[str, Any]
    config_schema: dict[str, Any]
    code_s3_key: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)


class AgentStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class MemoryLayer(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    META = "meta"


# ============================================================
# Domain / CKG types (Phase 10) -- typed views over
# GET/PATCH /api/v1/ckg/domains, /ckg/sub-graphs, /ckg/extract.
# Mirrors the exact response shapes of
# control-plane/backend/app/api/v1/endpoints/ckg.py, not an invented
# schema -- see vouchstone_sdk/domain.py::DomainClient.
# ============================================================

@dataclass
class Domain:
    """One row of the kg_domains registry -- a department/domain node in
    the tenant's auto-taxonomy tree (see app/services/kg_domains.py)."""
    slug: str
    name: str
    description: str = ""
    icon: str = "database"
    color: str = "#6b7280"
    parent_slug: str | None = None
    is_seed: bool = False
    created_by: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Domain":
        return cls(
            slug=data["slug"], name=data.get("name", data["slug"]),
            description=data.get("description", ""), icon=data.get("icon", "database"),
            color=data.get("color", "#6b7280"), parent_slug=data.get("parent_slug"),
            is_seed=bool(data.get("is_seed", False)), created_by=data.get("created_by"),
        )


@dataclass
class SubGraphSummary:
    """One card from GET /ckg/sub-graphs -- a domain plus its real,
    computed node/edge counts and health score (see
    app/services/sub_graphs.py::list_sub_graphs / compute_domain_health)."""
    slug: str
    name: str
    description: str = ""
    icon: str = "database"
    color: str = "#6b7280"
    parent_slug: str | None = None
    is_seed: bool = False
    node_count: int = 0
    edge_count: int = 0
    health: float = 0.0
    health_breakdown: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubGraphSummary":
        return cls(
            slug=data["slug"], name=data.get("name", data["slug"]),
            description=data.get("description", ""), icon=data.get("icon", "database"),
            color=data.get("color", "#6b7280"), parent_slug=data.get("parent_slug"),
            is_seed=bool(data.get("is_seed", False)), node_count=data.get("node_count", 0),
            edge_count=data.get("edge_count", 0), health=data.get("health", 0.0),
            health_breakdown=data.get("health_breakdown", {}),
        )


@dataclass
class SubGraphNode:
    id: str
    kind: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    status: str | None = None
    regulator_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubGraphNode":
        return cls(
            id=data["id"], kind=data.get("kind", ""), label=data.get("label", ""),
            attributes=data.get("attributes", {}) or {}, confidence=data.get("confidence", 1.0),
            status=data.get("status"), regulator_tags=data.get("regulator_tags", []) or [],
        )


@dataclass
class SubGraphEdge:
    id: str
    source: str
    target: str
    relation: str
    confidence: float = 1.0
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubGraphEdge":
        return cls(
            id=data["id"], source=data.get("source", ""), target=data.get("target", ""),
            relation=data.get("relation", ""), confidence=data.get("confidence", 1.0),
            attributes=data.get("attributes", {}) or {},
        )


@dataclass
class SubGraph:
    """Nodes + edges for one domain, from GET /ckg/sub-graphs/{slug} --
    includes every descendant domain's nodes too when ``slug`` has
    registered children (a department rollup), deduplicated by node id."""
    slug: str
    name: str
    description: str = ""
    icon: str = "database"
    color: str = "#6b7280"
    parent_slug: str | None = None
    nodes: list[SubGraphNode] = field(default_factory=list)
    edges: list[SubGraphEdge] = field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubGraph":
        return cls(
            slug=data["slug"], name=data.get("name", data["slug"]),
            description=data.get("description", ""), icon=data.get("icon", "database"),
            color=data.get("color", "#6b7280"), parent_slug=data.get("parent_slug"),
            nodes=[SubGraphNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[SubGraphEdge.from_dict(e) for e in data.get("edges", [])],
            total_nodes=data.get("total_nodes", 0), total_edges=data.get("total_edges", 0),
        )


@dataclass
class ClassifyResult:
    """Response from POST /ckg/domains/classify -- a backfill pass over any
    PROMOTED node with no domain assigned yet. Idempotent; already-classified
    nodes are left untouched. ``proposed_domains`` is slug -> display-name
    for any domain the LLM proposed for the first time during this pass
    (see app/services/domain_classifier.py::ClassificationResult) -- it is
    a mapping, not a list, because a proposed slug always comes with a
    human-readable name attached."""
    classified: int = 0
    proposed_domains: dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractionJob:
    """A CKG extraction job -- from POST /ckg/extract or
    GET /ckg/extractions/{job_id}. ``status`` is one of "running",
    "succeeded", "failed" (see app/models/ckg.py::ExtractionJobStatus)."""
    id: str
    status: str
    passes_completed: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    document_metadata: dict[str, Any] = field(default_factory=dict)
    engagement_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionJob":
        return cls(
            id=data["id"], status=data["status"], passes_completed=data.get("passes_completed", 0),
            started_at=data.get("started_at"), finished_at=data.get("finished_at"),
            error=data.get("error"), document_metadata=data.get("document_metadata", {}) or {},
            engagement_id=data.get("engagement_id"),
        )
