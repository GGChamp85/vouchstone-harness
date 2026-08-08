"""EntityGraph / PolicyGraph / WorkflowTrace — the three-compartment pattern.

Every accountable agent use case (AP-invoice matching, compliance evidence,
data migration, ...) decomposes into the same three compartments:

1. **EntityGraph**  — domain entities and the edges between them (what the
   agent knows: invoices, vendors, POs; controls, evidence, frameworks;
   tables, columns, pipelines; ...). Built from ``Entity`` (see types.py).
2. **PolicyGraph**   — a stable ruleset the agent's proposed actions are
   checked against (permit/forbid + obligations), independent of any one
   domain. Mirrors the shape of the control plane's ABAC engine
   (``app/services/abac.py``) so a policy decision made locally in the
   harness and one made by the hosted gateway are directly comparable.
3. **WorkflowTrace** — an append-only, hash-chained record of what actually
   happened, so a decision can be replayed and verified without a live
   connection to the control plane. Uses the same canonical-JSON + SHA-256
   chaining scheme as the control plane's ledger
   (``app/services/ledger_signing.py``): ``sha256(prev_hash || canonical_json(entry))``.

Before this module, "Specialize" only meant prompt + tools + scoped_subgraph
(see ``AgentConfig.scoped_subgraph``); a use case that needed real domain
structure, a policy check, or a verifiable trace had to hand-roll it. These
three classes formalize that pattern as an SDK primitive: define entities,
define policies, and every ``run()`` call automatically gets an append-only,
verifiable record — no per-use-case SDK code required.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import Entity

# ============================================================
# Canonical JSON + hash chaining
#
# Identical algorithm to control-plane/backend/app/services/ledger_signing.py
# (canonical_json / compute_entry_hash) so a hash computed locally in the
# harness and one computed by the hosted ledger are byte-for-byte
# comparable -- this is what makes a WorkflowTrace independently verifiable
# rather than "trust the SDK's math."
# ============================================================

def canonical_json(payload: dict[str, Any]) -> bytes:
    """RFC 8785-ish canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def compute_entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """sha256(prev_hash || canonical_json(payload))."""
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(canonical_json(payload))
    return h.hexdigest()


# ============================================================
# EntityGraph — domain entities + edges
# ============================================================

@dataclass
class Edge:
    """A directed, typed relationship between two entities."""
    source_id: str
    target_id: str
    edge_type: str
    attributes: dict[str, Any] = field(default_factory=dict)


class EntityGraph:
    """In-memory domain entity graph: nodes are ``Entity`` records, edges are
    typed relationships between them. Not a replacement for the control
    plane's Customer Knowledge Graph -- this is the client-side working set
    an agent reasons over during a single run, typically seeded from
    ``MemoryContext.semantic_entities`` and grown as the agent extracts new
    facts.
    """

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._edges: list[Edge] = []

    def add_entity(self, entity: Entity) -> Entity:
        self._entities[entity.id] = entity
        return entity

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def entities_by_type(self, entity_type: str) -> list[Entity]:
        return [e for e in self._entities.values() if e.entity_type == entity_type]

    def add_edge(
        self, source_id: str, target_id: str, edge_type: str,
        attributes: dict[str, Any] | None = None,
    ) -> Edge:
        if source_id not in self._entities:
            raise ValueError(f"unknown source entity: {source_id}")
        if target_id not in self._entities:
            raise ValueError(f"unknown target entity: {target_id}")
        edge = Edge(source_id=source_id, target_id=target_id, edge_type=edge_type,
                     attributes=attributes or {})
        self._edges.append(edge)
        return edge

    def related(self, entity_id: str, edge_type: str | None = None) -> list[Entity]:
        """Entities reachable from ``entity_id`` via one outbound edge,
        optionally filtered by edge type."""
        out: list[Entity] = []
        for e in self._edges:
            if e.source_id != entity_id:
                continue
            if edge_type is not None and e.edge_type != edge_type:
                continue
            target = self._entities.get(e.target_id)
            if target is not None:
                out.append(target)
        return out

    def __len__(self) -> int:
        return len(self._entities)

    def all_entities(self) -> list[Entity]:
        return list(self._entities.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": [
                {
                    "id": e.id, "entity_type": e.entity_type, "entity_key": e.entity_key,
                    "attributes": e.attributes, "confidence": e.confidence,
                    "source_trace_id": e.source_trace_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in self._entities.values()
            ],
            "edges": [
                {"source_id": ed.source_id, "target_id": ed.target_id,
                 "edge_type": ed.edge_type, "attributes": ed.attributes}
                for ed in self._edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityGraph:
        graph = cls()
        for ed in data.get("entities", []):
            created_at = ed.get("created_at")
            graph.add_entity(Entity(
                id=ed["id"], entity_type=ed["entity_type"], entity_key=ed["entity_key"],
                attributes=ed.get("attributes", {}), confidence=ed.get("confidence", 1.0),
                source_trace_id=ed.get("source_trace_id"),
                created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(timezone.utc),
            ))
        for edd in data.get("edges", []):
            graph.add_edge(edd["source_id"], edd["target_id"], edd["edge_type"],
                            edd.get("attributes"))
        return graph


# ============================================================
# PolicyGraph — permit/forbid ruleset with obligations
#
# Same effect/priority/condition shape as app/services/abac.py's evaluator
# so a policy authored once (e.g. in the control plane) can be exported and
# evaluated identically here, offline, by the compatibility gate (C7).
# ============================================================

_CONDITION_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda v, target: v == target,
    "ne": lambda v, target: v != target,
    "in": lambda v, target: v in (target or []),
    "not_in": lambda v, target: v not in (target or []),
    "gt": lambda v, target: v is not None and v > target,
    "gte": lambda v, target: v is not None and v >= target,
    "lt": lambda v, target: v is not None and v < target,
    "lte": lambda v, target: v is not None and v <= target,
    "startswith": lambda v, target: isinstance(v, str) and v.startswith(target),
    "regex": lambda v, target: isinstance(v, str) and re.search(target, v) is not None,
}


def _get_path(root: dict[str, Any], path: str) -> Any:
    cur: Any = root
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


@dataclass
class Policy:
    """One permit/forbid rule. ``priority`` breaks ties (lower runs first);
    the first matching ``forbid`` wins outright, otherwise obligations union
    across every matching ``permit``."""
    name: str
    effect: str  # "permit" | "forbid"
    action: dict[str, Any] | None = None  # e.g. {"eq": "schema.alter_table"}
    resource: dict[str, Any] | None = None  # e.g. {"data_classification": "pii"}
    conditions: list[dict[str, Any]] = field(default_factory=list)  # [{"path", "op", "value"}]
    obligations: list[str] = field(default_factory=list)
    priority: int = 100


@dataclass
class PolicyDecision:
    allow: bool
    obligations: list[str] = field(default_factory=list)
    matched_policy_names: list[str] = field(default_factory=list)
    reason: str = ""


def _matches_action(spec: dict[str, Any] | None, action: str) -> bool:
    if not spec:
        return True
    if "eq" in spec and action != spec["eq"]:
        return False
    if "in" in spec and action not in spec["in"]:
        return False
    if "startswith" in spec and not action.startswith(spec["startswith"]):
        return False
    return True


def _matches_resource(spec: dict[str, Any] | None, resource: dict[str, Any]) -> bool:
    if not spec:
        return True
    for key, target in spec.items():
        value = resource.get(key)
        if isinstance(target, list):
            if value not in target:
                return False
        else:
            if value != target:
                return False
    return True


def _matches_conditions(conditions: list[dict[str, Any]], root: dict[str, Any]) -> bool:
    for cond in conditions:
        path, op, target = cond.get("path"), cond.get("op"), cond.get("value")
        if not path or op not in _CONDITION_OPS:
            return False
        if not _CONDITION_OPS[op](_get_path(root, path), target):
            return False
    return True


class PolicyGraph:
    """A stable, evaluable ruleset. Add policies once; evaluate as many
    proposed actions against them as needed. Deny-by-default: an action
    with no matching permit policy is denied, matching the control plane's
    ABAC posture (see app/services/abac.py's "No matching permit policy
    (default deny)")."""

    def __init__(self) -> None:
        self._policies: list[Policy] = []

    def add_policy(self, policy: Policy) -> Policy:
        self._policies.append(policy)
        return policy

    def evaluate(
        self, *, principal: dict[str, Any], action: str,
        resource: dict[str, Any] | None = None, context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        resource = resource or {}
        context = context or {}
        root = {"principal": principal, "action": action, "resource": resource, "context": context}

        obligations: list[str] = []
        matched: list[str] = []
        any_permit = False

        for policy in sorted(self._policies, key=lambda p: (p.priority, p.name)):
            if not _matches_action(policy.action, action):
                continue
            if not _matches_resource(policy.resource, resource):
                continue
            if not _matches_conditions(policy.conditions, root):
                continue

            matched.append(policy.name)
            if policy.effect == "forbid":
                return PolicyDecision(
                    allow=False, obligations=[], matched_policy_names=matched,
                    reason=f"forbidden by policy: {policy.name}",
                )
            any_permit = True
            obligations.extend(o for o in policy.obligations if o not in obligations)

        if not any_permit:
            return PolicyDecision(
                allow=False, obligations=[], matched_policy_names=matched,
                reason="no matching permit policy (default deny)",
            )
        return PolicyDecision(allow=True, obligations=obligations, matched_policy_names=matched, reason="allow")


# ============================================================
# WorkflowTrace — append-only, hash-chained record
# ============================================================

@dataclass
class WorkflowTraceEntry:
    sequence: int
    kind: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str
    timestamp: str


class WorkflowTrace:
    """Append-only, hash-chained trace of what an agent (or the
    compatibility gate) actually did. Each entry's hash covers the previous
    entry's hash plus its own canonical payload, so the chain can be
    verified independently -- tampering with or reordering any entry breaks
    every hash after it. This is the client-side counterpart to the control
    plane's signed ledger (``app/services/ledger_signing.py``); a bundle
    pulled offline (C4) carries its own trace that can be verified without
    calling home, and reconciles with the hosted ledger on next sync.
    """

    def __init__(self) -> None:
        self._entries: list[WorkflowTraceEntry] = []

    def append(self, kind: str, payload: dict[str, Any], actor: str = "agent") -> WorkflowTraceEntry:
        prev_hash = self._entries[-1].entry_hash if self._entries else ""
        sequence = len(self._entries) + 1
        timestamp = datetime.now(timezone.utc).isoformat()
        body = {
            "sequence": sequence, "kind": kind, "actor": actor,
            "payload": payload, "timestamp": timestamp,
        }
        entry_hash = compute_entry_hash(prev_hash, body)
        entry = WorkflowTraceEntry(
            sequence=sequence, kind=kind, actor=actor, payload=payload,
            prev_hash=prev_hash, entry_hash=entry_hash, timestamp=timestamp,
        )
        self._entries.append(entry)
        return entry

    @property
    def tip_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else ""

    @property
    def entries(self) -> list[WorkflowTraceEntry]:
        return list(self._entries)

    def verify_chain(self) -> bool:
        """Recompute every hash from scratch and confirm the chain is intact."""
        prev_hash = ""
        for entry in self._entries:
            body = {
                "sequence": entry.sequence, "kind": entry.kind, "actor": entry.actor,
                "payload": entry.payload, "timestamp": entry.timestamp,
            }
            if entry.prev_hash != prev_hash:
                return False
            if compute_entry_hash(prev_hash, body) != entry.entry_hash:
                return False
            prev_hash = entry.entry_hash
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tip_hash": self.tip_hash,
            "entries": [
                {
                    "sequence": e.sequence, "kind": e.kind, "actor": e.actor,
                    "payload": e.payload, "prev_hash": e.prev_hash,
                    "entry_hash": e.entry_hash, "timestamp": e.timestamp,
                }
                for e in self._entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowTrace:
        trace = cls()
        trace._entries = [
            WorkflowTraceEntry(
                sequence=e["sequence"], kind=e["kind"], actor=e["actor"],
                payload=e["payload"], prev_hash=e["prev_hash"],
                entry_hash=e["entry_hash"], timestamp=e["timestamp"],
            )
            for e in data.get("entries", [])
        ]
        return trace
