"""DomainClient (Phase 10) -- exercises the real HTTP client against a
transport-level mock (httpx.MockTransport), not a fake SDK-level stub: every
request actually goes through DomainClient's method -> httpx.AsyncClient ->
transport path, and assertions check the real request that was built
(method, URL, params, JSON body) alongside the parsed response. This module
is also exercised against a live control plane in the E2E script's SDK step
(scripts/e2e-test.py) -- these tests cover the request/response contract in
isolation and in CI without a running backend.
"""
from __future__ import annotations

import json

import httpx
import pytest

from vouchstone_sdk.domain import DomainClient
from vouchstone_sdk.types import ClassifyResult, Domain, ExtractionJob, SubGraph, SubGraphSummary


def _client(handler) -> DomainClient:
    dc = DomainClient(
        api_key="test-key", control_plane_url="https://cp.example.com", tenant_id="tenant-1",
    )
    dc._client = httpx.AsyncClient(
        base_url=dc.base_url,
        headers=dc._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return dc


@pytest.mark.asyncio
async def test_list_domains_hits_real_endpoint_and_parses_rows():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"domains": [
            {"slug": "finance", "name": "Finance", "description": "Budgets",
             "icon": "dollar-sign", "color": "#84cc16", "parent_slug": None,
             "is_seed": True, "created_by": "seed"},
        ]})

    async with _client(handler) as dc:
        domains = await dc.list_domains()

    assert seen["method"] == "GET"
    assert seen["url"] == "https://cp.example.com/api/v1/ckg/domains?tenant_id=tenant-1"
    assert domains == [Domain(
        slug="finance", name="Finance", description="Budgets", icon="dollar-sign",
        color="#84cc16", parent_slug=None, is_seed=True, created_by="seed",
    )]


@pytest.mark.asyncio
async def test_curate_domain_omits_parent_slug_when_not_provided():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "slug": "finance", "name": "Finance & Accounting", "description": "Budgets",
            "icon": "dollar-sign", "color": "#84cc16", "parent_slug": None,
            "is_seed": True, "created_by": "user",
        })

    async with _client(handler) as dc:
        domain = await dc.curate_domain("finance", name="Finance & Accounting")

    assert seen["method"] == "PATCH"
    assert seen["url"].endswith("/api/v1/ckg/domains/finance?tenant_id=tenant-1")
    # parent_slug was never passed -> must be omitted entirely, not sent as
    # null, so the server's "leave alone" sentinel default applies.
    assert "parent_slug" not in seen["body"]
    assert seen["body"] == {"name": "Finance & Accounting"}
    assert domain.name == "Finance & Accounting"


@pytest.mark.asyncio
async def test_curate_domain_sends_explicit_none_to_clear_parent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "slug": "code", "name": "Code Graph", "description": "", "icon": "code",
            "color": "#3b82f6", "parent_slug": None, "is_seed": True, "created_by": "user",
        })

    async with _client(handler) as dc:
        await dc.curate_domain("code", parent_slug=None)

    assert seen["body"] == {"parent_slug": None}


@pytest.mark.asyncio
async def test_classify_parses_dict_shaped_proposed_domains():
    """proposed_domains is slug -> display-name (see
    app/services/domain_classifier.py::ClassificationResult), not a list --
    a real response shape, not a made-up one."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/ckg/domains/classify"
        return httpx.Response(200, json={
            "classified": 3, "proposed_domains": {"logistics": "Logistics"},
        })

    async with _client(handler) as dc:
        result = await dc.classify()

    assert result == ClassifyResult(classified=3, proposed_domains={"logistics": "Logistics"})


@pytest.mark.asyncio
async def test_list_sub_graphs_and_get_sub_graph():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/ckg/sub-graphs":
            return httpx.Response(200, json={"sub_graphs": [
                {"slug": "finance", "name": "Finance", "description": "", "icon": "dollar-sign",
                 "color": "#84cc16", "parent_slug": None, "is_seed": True,
                 "node_count": 4, "edge_count": 2, "health": 87.5, "health_breakdown": {"a": 1}},
            ]})
        assert request.url.path == "/api/v1/ckg/sub-graphs/finance"
        assert request.url.params["confidence_min"] == "0.5"
        return httpx.Response(200, json={
            "slug": "finance", "name": "Finance", "description": "", "icon": "dollar-sign",
            "color": "#84cc16", "parent_slug": None,
            "nodes": [{"id": "n1", "kind": "system", "label": "NetSuite", "attributes": {},
                       "confidence": 0.9, "status": "promoted", "regulator_tags": []}],
            "edges": [],
            "total_nodes": 1, "total_edges": 0,
        })

    async with _client(handler) as dc:
        summaries = await dc.list_sub_graphs()
        assert summaries == [SubGraphSummary(
            slug="finance", name="Finance", description="", icon="dollar-sign",
            color="#84cc16", parent_slug=None, is_seed=True, node_count=4, edge_count=2,
            health=87.5, health_breakdown={"a": 1},
        )]

        sub_graph = await dc.get_sub_graph("finance", confidence_min=0.5)
        assert isinstance(sub_graph, SubGraph)
        assert sub_graph.total_nodes == 1
        assert sub_graph.nodes[0].label == "NetSuite"


@pytest.mark.asyncio
async def test_extract_documents_requires_at_least_one_document():
    async with _client(lambda r: httpx.Response(200, json={})) as dc:
        with pytest.raises(ValueError):
            await dc.extract_documents([])


@pytest.mark.asyncio
async def test_extract_documents_posts_real_body_and_parses_job():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "job-1", "status": "running", "passes_completed": 0,
            "started_at": "2026-01-01T00:00:00Z", "finished_at": None, "error": None,
            "document_metadata": {}, "engagement_id": None,
        })

    async with _client(handler) as dc:
        job = await dc.extract_documents(
            [{"filename": "a.md", "content": "hello"}], engagement_id="eng-1",
        )

    assert seen["body"] == {
        "documents": [{"filename": "a.md", "content": "hello"}],
        "engagement_id": "eng-1",
    }
    assert job == ExtractionJob(
        id="job-1", status="running", passes_completed=0,
        started_at="2026-01-01T00:00:00Z", finished_at=None, error=None,
        document_metadata={}, engagement_id=None,
    )


@pytest.mark.asyncio
async def test_wait_for_extraction_polls_until_terminal_status():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        status = "running" if calls["n"] < 3 else "succeeded"
        return httpx.Response(200, json={
            "id": "job-1", "status": status, "passes_completed": calls["n"],
            "started_at": None, "finished_at": None, "error": None,
            "document_metadata": {}, "engagement_id": None,
        })

    async with _client(handler) as dc:
        job = await dc.wait_for_extraction("job-1", poll_interval=0.01, timeout=5.0)

    assert job.status == "succeeded"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_wait_for_extraction_times_out_on_stuck_job():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "job-1", "status": "running", "passes_completed": 1,
            "started_at": None, "finished_at": None, "error": None,
            "document_metadata": {}, "engagement_id": None,
        })

    async with _client(handler) as dc:
        with pytest.raises(TimeoutError):
            await dc.wait_for_extraction("job-1", poll_interval=0.01, timeout=0.05)


@pytest.mark.asyncio
async def test_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    async with _client(handler) as dc:
        with pytest.raises(httpx.HTTPStatusError):
            await dc.get_sub_graph("does-not-exist")
