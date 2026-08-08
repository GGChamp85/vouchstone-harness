"""VaultClient -- request/response contract via httpx.MockTransport,
mirroring tests/test_domain.py's pattern: every call goes through the real
method -> httpx.AsyncClient -> transport path and the assertions check the
actual request built (method, URL, params, body/multipart). This module had
zero test coverage before, which is how the README shipped examples whose
argument shapes didn't match a single method signature.
"""
from __future__ import annotations

import json

import httpx
import pytest

from vouchstone_sdk.vault import VaultClient


def _client(handler) -> VaultClient:
    vc = VaultClient(
        api_key="test-key", control_plane_url="https://cp.example.com", tenant_id="tenant-1",
    )
    vc._client = httpx.AsyncClient(
        base_url=vc.base_url,
        headers=vc._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return vc


@pytest.mark.asyncio
async def test_upload_files_builds_multipart_with_filename_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read()
        return httpx.Response(200, json={"documents": [{"id": "doc-1"}]})

    async with _client(handler) as vault:
        result = await vault.upload_files(
            "vault-9",
            [{"filename": "notes.md", "content": b"# hi", "content_type": "text/markdown"}],
            layer="raw",
        )

    assert result == {"documents": [{"id": "doc-1"}]}
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/vaults/vault-9/upload"
    assert seen["params"]["tenant_id"] == "tenant-1"
    assert seen["params"]["layer"] == "raw"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b"notes.md" in seen["body"] and b"# hi" in seen["body"]


@pytest.mark.asyncio
async def test_approve_posts_document_ids_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"approved": 2})

    async with _client(handler) as vault:
        await vault.approve("vault-9", ["d1", "d2"])

    assert seen["path"] == "/api/v1/vaults/vault-9/approve"
    assert seen["body"] == {"document_ids": ["d1", "d2"]}


@pytest.mark.asyncio
async def test_ingest_is_keyword_only_target():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"status": "queued"})

    async with _client(handler) as vault:
        await vault.ingest("vault-9", target="kg")

    assert seen["path"] == "/api/v1/vaults/vault-9/ingest"
    assert seen["body"] == {"target": "kg"}

    # The old README example passed paths positionally -- that call shape
    # must fail loudly, not be silently accepted.
    async with _client(handler) as vault:
        with pytest.raises(TypeError):
            await vault.ingest("vault-9", ["a.md"], target="kg")  # type: ignore[misc]


@pytest.mark.asyncio
async def test_set_autopilot_uses_enabled_and_source_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as vault:
        await vault.set_autopilot("vault-9", enabled=True, source_id="slack")

    assert seen["method"] == "PUT"
    assert seen["path"] == "/api/v1/vaults/vault-9/autopilot"
    assert seen["body"] == {"enabled": True, "source_id": "slack"}


@pytest.mark.asyncio
async def test_get_document_by_id_with_optional_version():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"id": "doc-1", "content": "x"})

    async with _client(handler) as vault:
        doc = await vault.get_document("vault-9", "doc-1", version="abc123")

    assert doc["id"] == "doc-1"
    assert seen["path"] == "/api/v1/vaults/vault-9/documents/doc-1"
    assert seen["params"]["version"] == "abc123"


@pytest.mark.asyncio
async def test_search_passes_query_layer_limit():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [{"id": "doc-1"}]})

    async with _client(handler) as vault:
        results = await vault.search("vault-9", "invoices", layer="canonical", limit=5)

    assert results == [{"id": "doc-1"}]
    assert seen["params"]["q"] == "invoices"
    assert seen["params"]["layer"] == "canonical"
    assert seen["params"]["limit"] == "5"
