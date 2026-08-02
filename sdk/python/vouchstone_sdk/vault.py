"""Vouchstone SDK — Document Vault Client

Provides access to the 3-layer Document Vault (Raw / Workspace / Canonical)
that acts as a moderation gatekeeper for all connector data before it reaches
the Knowledge Graph, Wiki, or Company Brain.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import httpx


class VaultClient:
    """Client for the Vouchstone Document Vault API.

    The vault enforces a three-layer flow:
      Raw       — immutable exact replica of source data
      Workspace — user-editable moderation copy
      Canonical — approved content that feeds KG / Wiki / Brain

    Usage::

        async with VaultClient(
            api_key="sk-...",
            control_plane_url="https://your-control-plane-host.example.com",
            tenant_id="t-1",
        ) as vc:
            vaults = await vc.list_vaults()
            tree = await vc.list_tree(vault_id="v-1", layer="workspace")
            await vc.approve(vault_id="v-1", document_ids=["d-1", "d-2"])
    """

    def __init__(
        self,
        api_key: str,
        control_plane_url: str,
        tenant_id: Optional[str] = None,
        timeout: float = 60.0,
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
            timeout=timeout,
        )

    # ── helpers ────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"/api/v1/vault{path}"

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if self.tenant_id:
            params["tenant_id"] = self.tenant_id
        if extra:
            params.update(extra)
        return params

    # ── vault CRUD ─────────────────────────────────────────────────────

    async def list_vaults(self) -> List[Dict[str, Any]]:
        """List all vaults for the current tenant."""
        resp = await self._client.get(
            self._url("s"), params=self._params(),
        )
        resp.raise_for_status()
        return resp.json().get("vaults", resp.json())

    async def create_vault(
        self,
        name: str,
        *,
        description: str = "",
        connector_id: Optional[str] = None,
        visibility: str = "private",
    ) -> Dict[str, Any]:
        """Create a new vault."""
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "visibility": visibility,
        }
        if connector_id is not None:
            body["connector_id"] = connector_id
        resp = await self._client.post(
            self._url("s"), params=self._params(), json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_vault(self, vault_id: str) -> Dict[str, Any]:
        """Fetch a single vault by ID."""
        resp = await self._client.get(
            self._url(f"s/{vault_id}"), params=self._params(),
        )
        resp.raise_for_status()
        return resp.json()

    # ── document operations ────────────────────────────────────────────

    async def upload_files(
        self,
        vault_id: str,
        files: List[Dict[str, Any]],
        *,
        layer: str = "raw",
        path_prefix: str = "/",
    ) -> Dict[str, Any]:
        """Upload one or more files into a vault layer.

        Each entry in *files* must contain:
          - ``filename`` (str): the file name
          - ``content`` (bytes): file content
          - ``content_type`` (str, optional): MIME type

        Returns the API response with created document records.
        """
        multipart_files = []
        for f in files:
            ct = f.get("content_type", "application/octet-stream")
            multipart_files.append(
                ("files", (f["filename"], f["content"], ct))
            )
        resp = await self._client.post(
            self._url(f"s/{vault_id}/upload"),
            params=self._params({"layer": layer, "path_prefix": path_prefix}),
            files=multipart_files,
        )
        resp.raise_for_status()
        return resp.json()

    async def list_tree(
        self,
        vault_id: str,
        *,
        layer: str = "workspace",
        path: str = "/",
    ) -> List[Dict[str, Any]]:
        """List documents in the vault tree at *path* within *layer*."""
        resp = await self._client.get(
            self._url(f"s/{vault_id}/tree"),
            params=self._params({"layer": layer, "path": path}),
        )
        resp.raise_for_status()
        return resp.json().get("documents", resp.json())

    async def get_document(
        self,
        vault_id: str,
        document_id: str,
        *,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a single document (metadata + content).

        Optionally specify *version* (commit SHA) to retrieve a historical
        snapshot.
        """
        params = self._params()
        if version:
            params["version"] = version
        resp = await self._client.get(
            self._url(f"s/{vault_id}/documents/{document_id}"),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        vault_id: str,
        query: str,
        *,
        layer: str = "canonical",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Full-text / semantic search within a vault layer."""
        resp = await self._client.get(
            self._url(f"s/{vault_id}/search"),
            params=self._params({
                "q": query,
                "layer": layer,
                "limit": limit,
            }),
        )
        resp.raise_for_status()
        return resp.json().get("results", resp.json())

    # ── moderation actions ─────────────────────────────────────────────

    async def approve(
        self,
        vault_id: str,
        document_ids: List[str],
    ) -> Dict[str, Any]:
        """Approve documents — promotes them from Workspace to Canonical."""
        resp = await self._client.post(
            self._url(f"s/{vault_id}/approve"),
            params=self._params(),
            json={"document_ids": document_ids},
        )
        resp.raise_for_status()
        return resp.json()

    async def reject(
        self,
        vault_id: str,
        document_ids: List[str],
        *,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Reject documents — marks them as rejected in Workspace."""
        body: Dict[str, Any] = {"document_ids": document_ids}
        if reason:
            body["reason"] = reason
        resp = await self._client.post(
            self._url(f"s/{vault_id}/reject"),
            params=self._params(),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def ingest(
        self,
        vault_id: str,
        *,
        target: str = "all",
    ) -> Dict[str, Any]:
        """Trigger ingestion of Canonical documents into downstream systems.

        *target* controls which downstream systems receive the data:
        ``"all"``, ``"kg"``, ``"wiki"``, or ``"brain"``.
        """
        resp = await self._client.post(
            self._url(f"s/{vault_id}/ingest"),
            params=self._params(),
            json={"target": target},
        )
        resp.raise_for_status()
        return resp.json()

    # ── autopilot ──────────────────────────────────────────────────────

    async def set_autopilot(
        self,
        vault_id: str,
        *,
        enabled: bool,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Toggle auto-pilot mode for a vault or a specific source.

        When auto-pilot is enabled, incoming documents are automatically
        approved and promoted to Canonical without manual review.
        """
        body: Dict[str, Any] = {"enabled": enabled}
        if source_id is not None:
            body["source_id"] = source_id
        resp = await self._client.put(
            self._url(f"s/{vault_id}/autopilot"),
            params=self._params(),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    # ── lifecycle ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> VaultClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
