"""Tests for EntityGraph / PolicyGraph / WorkflowTrace (C6).

Acceptance criteria: the AP-invoice, compliance-evidence, and migration use
cases can each be expressed against this interface without SDK code
changes per use case. One test class per use case below proves that with
the *same* three primitives, no per-domain subclassing or special-casing.
"""
from datetime import datetime, timezone

import pytest

from vouchstone_sdk import (
    Entity,
    EntityGraph,
    Policy,
    PolicyGraph,
    WorkflowTrace,
    canonical_json,
    compute_entry_hash,
)


def _entity(entity_id, entity_type, key, **attrs):
    return Entity(
        id=entity_id, entity_type=entity_type, entity_key=key,
        attributes=attrs, confidence=1.0, source_trace_id=None,
        created_at=datetime.now(timezone.utc),
    )


# ============================================================
# EntityGraph
# ============================================================

def test_entity_graph_add_and_query():
    g = EntityGraph()
    g.add_entity(_entity("inv-1", "invoice", "INV-001", amount=1200.0, vendor_id="ven-1"))
    g.add_entity(_entity("ven-1", "vendor", "Acme Corp"))
    g.add_edge("inv-1", "ven-1", "billed_by")

    assert len(g) == 2
    assert g.get_entity("inv-1").attributes["amount"] == 1200.0
    invoices = g.entities_by_type("invoice")
    assert len(invoices) == 1 and invoices[0].id == "inv-1"

    related = g.related("inv-1", edge_type="billed_by")
    assert len(related) == 1 and related[0].id == "ven-1"


def test_entity_graph_edge_requires_known_entities():
    g = EntityGraph()
    g.add_entity(_entity("a", "x", "A"))
    with pytest.raises(ValueError):
        g.add_edge("a", "does-not-exist", "rel")


def test_entity_graph_roundtrip_serialization():
    g = EntityGraph()
    g.add_entity(_entity("a", "table", "shipments"))
    g.add_entity(_entity("b", "column", "shipments.eta"))
    g.add_edge("b", "a", "belongs_to")

    data = g.to_dict()
    restored = EntityGraph.from_dict(data)
    assert len(restored) == 2
    assert restored.get_entity("b").entity_type == "column"
    assert len(restored.related("b", edge_type="belongs_to")) == 1


# ============================================================
# PolicyGraph
# ============================================================

def test_policy_graph_default_deny():
    pg = PolicyGraph()
    decision = pg.evaluate(principal={"role": "agent"}, action="invoice.approve")
    assert decision.allow is False
    assert "default deny" in decision.reason


def test_policy_graph_permit_with_obligations():
    pg = PolicyGraph()
    pg.add_policy(Policy(
        name="auto-approve small invoices",
        effect="permit",
        action={"eq": "invoice.approve"},
        conditions=[{"path": "resource.amount", "op": "lt", "value": 5000}],
        obligations=["log_to_audit"],
    ))
    decision = pg.evaluate(
        principal={"role": "ap_agent"}, action="invoice.approve",
        resource={"amount": 1200.0},
    )
    assert decision.allow is True
    assert decision.obligations == ["log_to_audit"]
    assert "auto-approve small invoices" in decision.matched_policy_names


def test_policy_graph_forbid_wins_over_permit():
    pg = PolicyGraph()
    pg.add_policy(Policy(
        name="permit all invoice approvals", effect="permit",
        action={"eq": "invoice.approve"}, priority=100,
    ))
    pg.add_policy(Policy(
        name="forbid over-budget approvals", effect="forbid",
        action={"eq": "invoice.approve"}, priority=10,
        conditions=[{"path": "resource.amount", "op": "gte", "value": 5000}],
    ))
    decision = pg.evaluate(
        principal={"role": "ap_agent"}, action="invoice.approve",
        resource={"amount": 9000.0},
    )
    assert decision.allow is False
    assert "forbid over-budget approvals" in decision.reason


def test_policy_graph_compliance_evidence_use_case():
    """A compliance-evidence agent: PII access requires dual review."""
    pg = PolicyGraph()
    pg.add_policy(Policy(
        name="pii access requires dual signoff", effect="permit",
        action={"startswith": "evidence."},
        resource={"data_classification": "pii"},
        obligations=["require_dual_signoff", "log_to_audit"],
    ))
    pg.add_policy(Policy(
        name="non-pii evidence auto-processed", effect="permit",
        action={"startswith": "evidence."},
        resource={"data_classification": "internal"},
        obligations=["log_to_audit"],
    ))
    pii_decision = pg.evaluate(
        principal={"role": "compliance_agent"}, action="evidence.collect",
        resource={"data_classification": "pii"},
    )
    assert pii_decision.allow and "require_dual_signoff" in pii_decision.obligations

    internal_decision = pg.evaluate(
        principal={"role": "compliance_agent"}, action="evidence.collect",
        resource={"data_classification": "internal"},
    )
    assert internal_decision.allow and "require_dual_signoff" not in internal_decision.obligations


# ============================================================
# WorkflowTrace
# ============================================================

def test_workflow_trace_chains_and_verifies():
    trace = WorkflowTrace()
    trace.append("migration.step_started", {"table": "shipments"}, actor="schema_mapper")
    trace.append("migration.step_completed", {"table": "shipments", "rows": 1200}, actor="schema_mapper")

    assert len(trace.entries) == 2
    assert trace.entries[0].prev_hash == ""
    assert trace.entries[1].prev_hash == trace.entries[0].entry_hash
    assert trace.tip_hash == trace.entries[-1].entry_hash
    assert trace.verify_chain() is True


def test_workflow_trace_detects_tampering():
    trace = WorkflowTrace()
    trace.append("action.approved", {"amount": 100})
    trace.append("action.approved", {"amount": 200})

    # Tamper with a stored payload without recomputing hashes.
    trace._entries[0].payload["amount"] = 999999
    assert trace.verify_chain() is False


def test_workflow_trace_roundtrip_serialization():
    trace = WorkflowTrace()
    trace.append("kind.a", {"x": 1})
    trace.append("kind.b", {"y": 2})

    restored = WorkflowTrace.from_dict(trace.to_dict())
    assert restored.tip_hash == trace.tip_hash
    assert restored.verify_chain() is True


def test_workflow_trace_matches_control_plane_hash_algorithm():
    """The exact algorithm used by app/services/ledger_signing.py:
    sha256(prev_hash || canonical_json(payload)) -- verified independently
    here so a locally-produced hash is directly comparable to one computed
    by the hosted ledger."""
    import hashlib
    payload = {"b": 2, "a": 1}
    expected = hashlib.sha256(
        b"" + b'{"a":1,"b":2}'
    ).hexdigest()
    assert canonical_json(payload) == b'{"a":1,"b":2}'
    assert compute_entry_hash("", payload) == expected


# ============================================================
# Cross-cutting: migration use case exercising all three together
# ============================================================

def test_migration_use_case_end_to_end():
    """Postgres-to-Databricks migration: entities are tables/columns, policy
    forbids dropping tables outside dev, trace records what happened."""
    graph = EntityGraph()
    graph.add_entity(_entity("shipments", "table", "shipments", row_count=50000))
    graph.add_entity(_entity("shipments.eta", "column", "eta", type="timestamp"))
    graph.add_edge("shipments.eta", "shipments", "belongs_to")

    policy = PolicyGraph()
    policy.add_policy(Policy(
        name="no drop outside dev", effect="forbid",
        action={"startswith": "schema.drop_"},
        conditions=[{"path": "context.environment", "op": "in", "value": ["uat", "prod"]}],
    ))
    policy.add_policy(Policy(
        name="permit schema changes in dev", effect="permit",
        action={"startswith": "schema."}, obligations=["log_to_audit"],
    ))

    trace = WorkflowTrace()

    decision = policy.evaluate(
        principal={"agent_id": "schema-mapper-1"}, action="schema.alter_table",
        context={"environment": "dev"},
    )
    assert decision.allow is True
    trace.append("schema.alter_table", {
        "table": graph.get_entity("shipments").entity_key,
        "decision": decision.allow,
    }, actor="schema-mapper-1")

    drop_decision = policy.evaluate(
        principal={"agent_id": "schema-mapper-1"}, action="schema.drop_table",
        context={"environment": "prod"},
    )
    assert drop_decision.allow is False
    trace.append("schema.drop_table_denied", {
        "table": graph.get_entity("shipments").entity_key,
        "reason": drop_decision.reason,
    }, actor="schema-mapper-1")

    assert trace.verify_chain() is True
    assert len(trace.entries) == 2
