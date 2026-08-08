"""Vouchstone SDK Client - Connection to Control Plane"""

from typing import Any

import httpx

from .types import AgentDefinition


class VouchstoneClient:
    """Client for communicating with Vouchstone Control Plane"""
    
    def __init__(
        self,
        api_key: str,
        control_plane_url: str,
        tenant_id: str | None = None
    ):
        # No default for control_plane_url: api.vouchstone.ai does not
        # resolve, it was a placeholder. Requiring it explicitly fails
        # loudly at construction instead of silently pointing at a dead host.
        self.api_key = api_key
        self.base_url = control_plane_url.rstrip("/")
        self.tenant_id = tenant_id
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Tenant-ID": tenant_id or "",
            },
            timeout=30.0
        )
    
    # ============================================================
    # Generic API-path helpers
    # ============================================================
    #
    # The memory layers (EpisodicMemory, ProceduralMemory, MetaMemory,
    # SemanticMemory) call ``api_client._get(path)`` / ``._post(path, body)``
    # with control-plane paths like "/memory-pipeline/skills/{agent_id}".
    # Until these existed, passing a VouchstoneClient as api_client crashed
    # with AttributeError on the first memory call -- the documented
    # control-plane-backed memory mode was unusable. Paths are relative to
    # /api/v1; tenant_id is attached automatically when the client has one.

    def _api_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if self.tenant_id:
            merged["tenant_id"] = self.tenant_id
        if params:
            merged.update(params)
        return merged

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET /api/v1{path} -> parsed JSON. ``path`` starts with '/'."""
        resp = await self._client.get(
            f"/api/v1{path}", params=self._api_params(params),
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST /api/v1{path} -> parsed JSON. ``path`` starts with '/'."""
        resp = await self._client.post(
            f"/api/v1{path}", json=body or {}, params=self._api_params(params),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_agent(self, agent_id: str) -> AgentDefinition:
        """Fetch agent definition from control plane"""
        response = await self._client.get(f"/api/v1/agents/{agent_id}")
        response.raise_for_status()
        return AgentDefinition(**response.json())
    
    async def list_agents(self) -> list[AgentDefinition]:
        """List all agents for tenant"""
        response = await self._client.get("/api/v1/agents")
        response.raise_for_status()
        return [AgentDefinition(**a) for a in response.json()["agents"]]
    
    async def report_metrics(self, metrics: dict[str, Any]) -> None:
        """Report metrics back to control plane"""
        await self._client.post("/api/v1/webhooks/data-plane/metrics", json=metrics)
    
    async def report_status(self, agent_id: str, status: dict[str, Any]) -> None:
        """Report agent status to control plane"""
        await self._client.post(
            "/api/v1/webhooks/data-plane/status",
            json={"agent_id": agent_id, **status}
        )
    
    # ============================================================
    # Slice 1 — data plane sync helpers
    # ============================================================

    async def heartbeat(
        self,
        runtime_version: str,
        pod_count: int,
        queue_depth: int,
        last_seq: int,
        runtime_token: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/data-plane/heartbeat — returns heartbeat response.

        ``runtime_token`` is required (provide either here or via
        ``X-Runtime-Token`` on the client).
        """
        headers = {"X-Runtime-Token": runtime_token or ""}
        body = {
            "tenant_id": self.tenant_id,
            "runtime_version": runtime_version,
            "pod_count": pod_count,
            "queue_depth": queue_depth,
            "last_ledger_seq_forwarded": last_seq,
        }
        resp = await self._client.post(
            "/api/v1/data-plane/heartbeat", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def replay_ledger(
        self,
        entries: list[dict[str, Any]],
        runtime_token: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/data-plane/ledger/replay — bulk replay."""
        headers = {"X-Runtime-Token": runtime_token or ""}
        body = {"tenant_id": self.tenant_id, "entries": entries}
        resp = await self._client.post(
            "/api/v1/data-plane/ledger/replay", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_agent_spec(
        self,
        agent_id: str | None = None,
        since: float = 0.0,
        runtime_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/data-plane/agents/spec?since=&tenant_id=.

        ``agent_id`` is optional — when supplied the response is filtered
        to that agent locally.
        """
        headers = {"X-Runtime-Token": runtime_token or ""}
        params: dict[str, Any] = {"tenant_id": self.tenant_id or "", "since": since}
        resp = await self._client.get(
            "/api/v1/data-plane/agents/spec", params=params, headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if agent_id:
            data["agents"] = [a for a in data.get("agents", []) if str(a.get("id")) == str(agent_id)]
        return data

    async def close(self):
        """Close the HTTP client"""
        await self._client.aclose()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self.close()
