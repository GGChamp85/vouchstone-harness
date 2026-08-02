"""Type definitions for Vouchstone SDK"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


@dataclass
class Message:
    """Input message to an agent"""
    content: str
    role: str = "user"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Decision:
    """A decision made by an agent"""
    id: str
    question: str
    answer: str
    confidence: float
    reasoning: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AgentResponse:
    """Response from an agent"""
    content: str
    decisions: List[Decision] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, int] = field(default_factory=dict)


@dataclass
class MemoryEntry:
    """An entry from memory search"""
    id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Episode:
    """An episodic memory entry"""
    id: str
    timestamp: datetime
    message: str
    response: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodicTrace:
    """A trace from the 5-layer episodic memory"""
    id: str
    session_id: str
    turn_number: int
    user_input: str
    agent_response: str
    tools_used: List[str] = field(default_factory=list)
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
    attributes: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    source_trace_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Skill:
    """A procedural skill in the memory graph"""
    id: str
    name: str
    description: str
    steps: List[str] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)
    prerequisites: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    execution_count: int = 0
    version: int = 1
    # Domain tags (e.g. "finance", "compliance", "migration") -- used by
    # AgentConfig.scoped_subgraph to constrain which skills an agent's
    # find_skill()/list_skills() calls can surface. Empty by default
    # (unscoped/matches nothing specific; only visible to unscoped agents).
    tags: List[str] = field(default_factory=list)


@dataclass
class MemoryContext:
    """Full context prepared by the memory pipeline before a turn"""
    working_memory: List[Dict[str, Any]] = field(default_factory=list)
    episodic_context: List[EpisodicTrace] = field(default_factory=list)
    semantic_entities: List[Entity] = field(default_factory=list)
    procedural_skills: List[Skill] = field(default_factory=list)
    scratchpad: Dict[str, Any] = field(default_factory=dict)


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
    per_layer: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    last_maintenance: Optional[datetime] = None


@dataclass
class AgentDefinition:
    """Agent definition from control plane"""
    id: str
    name: str
    slug: str
    agent_type: str
    status: str
    memory_config: Dict[str, Any]
    config_schema: Dict[str, Any]
    code_s3_key: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)


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
