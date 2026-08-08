"""AgentConfig.scoped_domains (Phase 3) -- KG-domain scoping for the SDK's
semantic/procedural memory queries, consistent with the control plane's
kg_scope.domains semantics (app/services/company_brain.py::_matches_kg_scope)
but a flat match only (see agent.py's field docstring for why there's no
offline hierarchy expansion).
"""
import pytest

from vouchstone_sdk.memory import ProceduralMemory, SemanticMemory
from vouchstone_sdk.types import Entity, Skill


@pytest.mark.asyncio
async def test_search_entities_filters_by_scoped_domains():
    mem = SemanticMemory()  # no api_client / chroma -> local dict backend
    await mem.upsert_entity("agent-1", Entity(
        id="e1", entity_type="invoice", entity_key="inv-1",
        attributes={"domains": ["finance"]},
    ))
    await mem.upsert_entity("agent-1", Entity(
        id="e2", entity_type="server", entity_key="srv-1",
        attributes={"domains": ["infrastructure"]},
    ))

    results = await mem.search_entities("agent-1", "inv", allowed_domains=["finance"])
    assert {e.id for e in results} == {"e1"}

    results_all = await mem.search_entities("agent-1", "inv")  # unscoped
    assert {e.id for e in results_all} == {"e1"}


@pytest.mark.asyncio
async def test_list_entities_filters_by_scoped_domains():
    mem = SemanticMemory()
    await mem.upsert_entity("agent-1", Entity(
        id="e1", entity_type="invoice", entity_key="inv-1",
        attributes={"domains": ["finance"]},
    ))
    await mem.upsert_entity("agent-1", Entity(
        id="e2", entity_type="server", entity_key="srv-1",
        attributes={"domains": ["infrastructure"]},
    ))

    results = await mem.list_entities("agent-1", allowed_domains=["finance"])
    assert {e.id for e in results} == {"e1"}

    results_none = await mem.list_entities("agent-1", allowed_domains=["hr"])
    assert results_none == []


@pytest.mark.asyncio
async def test_entity_with_no_domains_attribute_is_excluded_by_a_domain_scope():
    """An entity that predates domain tagging (no "domains" key at all)
    must not leak into a domain-scoped query just because the key is
    missing -- absence is not a wildcard match."""
    mem = SemanticMemory()
    await mem.upsert_entity("agent-1", Entity(
        id="e1", entity_type="invoice", entity_key="inv-1", attributes={},
    ))
    results = await mem.search_entities("agent-1", "inv", allowed_domains=["finance"])
    assert results == []


@pytest.mark.asyncio
async def test_find_skill_filters_by_scoped_domains_via_tags():
    proc = ProceduralMemory()
    await proc.register_skill("agent-1", Skill(
        id="s1", name="reconcile-invoices", description="Reconcile AP invoices",
        tags=["finance"],
    ))
    await proc.register_skill("agent-1", Skill(
        id="s2", name="restart-service", description="Restart a hung service",
        tags=["infrastructure"],
    ))

    results = await proc.find_skill("agent-1", "reconcile", allowed_domains=["finance"])
    assert {s.id for s in results} == {"s1"}

    # A skill tagged into the requested domain but not matching the text
    # query at all is correctly excluded by the query, independent of scope.
    results_mismatched_query = await proc.find_skill("agent-1", "reconcile", allowed_domains=["infrastructure"])
    assert results_mismatched_query == []


@pytest.mark.asyncio
async def test_list_skills_combines_tag_scope_and_domain_scope():
    proc = ProceduralMemory()
    await proc.register_skill("agent-1", Skill(
        id="s1", name="reconcile-invoices", description="", tags=["finance", "urgent"],
    ))
    await proc.register_skill("agent-1", Skill(
        id="s2", name="close-books", description="", tags=["finance"],
    ))
    await proc.register_skill("agent-1", Skill(
        id="s3", name="restart-service", description="", tags=["infrastructure"],
    ))

    finance_only = await proc.list_skills("agent-1", allowed_domains=["finance"])
    assert {s.id for s in finance_only} == {"s1", "s2"}

    finance_and_urgent = await proc.list_skills(
        "agent-1", allowed_domains=["finance"], allowed_tags=["urgent"],
    )
    assert {s.id for s in finance_and_urgent} == {"s1"}
