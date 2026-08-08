"""VouchstoneClient -- request-building contract via httpx.MockTransport,
including the generic _get/_post API-path helpers the memory layers depend
on (EpisodicMemory/ProceduralMemory/MetaMemory all call api_client._get /
._post; before these helpers existed, passing a VouchstoneClient as
api_client raised AttributeError on the first memory call).
"""
from __future__ import annotations

import json

import httpx
import pytest

from vouchstone_sdk.client import VouchstoneClient


def _client(handler) -> VouchstoneClient:
    vc = VouchstoneClient(
        api_key="test-key", control_plane_url="https://cp.example.com", tenant_id="tenant-1",
    )
    vc._client = httpx.AsyncClient(
        base_url=vc.base_url,
        headers=vc._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return vc


@pytest.mark.asyncio
async def test_generic_get_prefixes_api_v1_and_attaches_tenant():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"skills": []})

    vc = _client(handler)
    resp = await vc._get("/memory-pipeline/skills/agent-1", params={"query": "x"})
    await vc.close()

    assert resp == {"skills": []}
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/memory-pipeline/skills/agent-1"
    assert seen["params"] == {"tenant_id": "tenant-1", "query": "x"}
    assert seen["auth"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_generic_post_sends_json_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"episodic_trace_id": "t-1"})

    vc = _client(handler)
    resp = await vc._post("/memory-pipeline/process-turn", {"agent_id": "a1"})
    await vc.close()

    assert resp["episodic_trace_id"] == "t-1"
    assert seen["path"] == "/api/v1/memory-pipeline/process-turn"
    assert seen["params"] == {"tenant_id": "tenant-1"}
    assert seen["body"] == {"agent_id": "a1"}


@pytest.mark.asyncio
async def test_generic_get_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    vc = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await vc._get("/memory-pipeline/health/agent-1")
    await vc.close()


@pytest.mark.asyncio
async def test_heartbeat_posts_runtime_token_and_tenant_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("x-runtime-token")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    vc = _client(handler)
    await vc.heartbeat("1.5.0", pod_count=2, queue_depth=0, last_seq=41,
                       runtime_token="rt-secret")
    await vc.close()

    assert seen["path"] == "/api/v1/data-plane/heartbeat"
    assert seen["token"] == "rt-secret"
    assert seen["body"]["tenant_id"] == "tenant-1"
    assert seen["body"]["last_ledger_seq_forwarded"] == 41


@pytest.mark.asyncio
async def test_fetch_agent_spec_filters_locally_by_agent_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agents": [
            {"id": "a1", "name": "one"}, {"id": "a2", "name": "two"},
        ]})

    vc = _client(handler)
    data = await vc.fetch_agent_spec(agent_id="a2")
    await vc.close()

    assert [a["id"] for a in data["agents"]] == ["a2"]
