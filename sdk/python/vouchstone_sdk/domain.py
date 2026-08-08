"""Vouchstone SDK — Knowledge-Graph Domain Builder

Talks to the real, mounted ``/api/v1/ckg`` endpoints (see
``control-plane/backend/app/api/v1/endpoints/ckg.py``) backing the
``kg_domains`` registry (``app/services/kg_domains.py``), per-domain
sub-graph views (``app/services/sub_graphs.py``), and LLM auto-taxonomy
classification (``app/services/domain_classifier.py``).

Domains on this platform are **auto-taxonomy**, not a manually-defined
ontology a human authors up front: the extraction pipeline classifies each
promoted ``KGNode`` into one or more domain slugs as it's promoted, and any
genuinely new slug the LLM proposes is persisted as a real registry row
(name/description/icon/color/hierarchy) the instant it's proposed -- there
is no separate "create a domain" step. What a human (or this client) can do
is: retry classification for anything that missed it (``classify()``),
curate an existing domain's display metadata or hierarchy
(``curate_domain()``), and browse the resulting per-domain sub-graph
(``list_domains()`` / ``list_sub_graphs()`` / ``get_sub_graph()``).

Fluent flow this module completes end to end, matching the platform's
Ground -> Specialize story::

    connect                    VouchstoneClient / DomainClient / VaultClient
    -> vault                   VaultClient: upload_files -> approve
    -> extract                 VaultClient.ingest(target="kg") for
                                vault-approved docs, or
                                DomainClient.extract_documents() for raw
                                documents that skip the vault moderation step
    -> domain KG                DomainClient.classify() to backfill any
                                unclassified nodes, then list_domains() /
                                list_sub_graphs() / get_sub_graph() to read
                                the resulting per-domain knowledge graph

See the README's "DomainClient" section for a runnable example against a
live control plane.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .types import (
    ClassifyResult,
    Domain,
    ExtractionJob,
    SubGraph,
    SubGraphSummary,
)

# Sentinel for "parent_slug not provided" -- mirrors the control plane's
# DomainUpdateIn "__unset__" default (app/api/v1/endpoints/ckg.py) so
# "leave the parent alone" (omit the key) and "clear to root" (pass
# parent_slug=None explicitly) stay distinguishable, exactly like the
# server-side contract.
_UNSET = object()

_TERMINAL_JOB_STATUSES = ("succeeded", "failed")


class DomainClient:
    """Client for the Vouchstone Knowledge-Graph domain builder.

    Usage::

        async with DomainClient(
            api_key="sk-...",
            control_plane_url="https://your-control-plane-host.example.com",
            tenant_id="t-1",
        ) as dc:
            job = await dc.extract_documents([
                {"filename": "runbook.md", "content": "..."},
            ])
            job = await dc.wait_for_extraction(job.id)

            result = await dc.classify()  # backfill any unclassified nodes
            domains = await dc.list_domains()
            sub_graphs = await dc.list_sub_graphs()
            finance_kg = await dc.get_sub_graph("finance")
    """

    def __init__(
        self,
        api_key: str,
        control_plane_url: str,
        tenant_id: str | None = None,
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
        return f"/api/v1/ckg{path}"

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.tenant_id:
            params["tenant_id"] = self.tenant_id
        if extra:
            params.update(extra)
        return params

    # ── domain registry ───────────────────────────────────────────────

    async def list_domains(self) -> list[Domain]:
        """GET /ckg/domains -- the tenant's full kg_domains registry tree
        (seeded idempotently server-side on first call: seed departments +
        every LLM-proposed domain + any user curation)."""
        resp = await self._client.get(self._url("/domains"), params=self._params())
        resp.raise_for_status()
        return [Domain.from_dict(d) for d in resp.json().get("domains", [])]

    async def curate_domain(
        self,
        slug: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        parent_slug: Any = _UNSET,
    ) -> Domain:
        """PATCH /ckg/domains/{slug} -- rename/re-describe/re-parent a
        domain. This never invents or deletes node-level classifications,
        only the registry's display metadata and hierarchy.

        ``name``/``description``/``icon``/``color`` of ``None`` mean "leave
        alone" (the server-side default). ``parent_slug`` defaults to the
        "not provided" sentinel (leave alone); pass ``parent_slug=None``
        explicitly to clear it to a root domain, or a slug string to
        re-parent under an existing registry domain.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if icon is not None:
            body["icon"] = icon
        if color is not None:
            body["color"] = color
        if parent_slug is not _UNSET:
            body["parent_slug"] = parent_slug

        resp = await self._client.patch(
            self._url(f"/domains/{slug}"), params=self._params(), json=body,
        )
        resp.raise_for_status()
        return Domain.from_dict(resp.json())

    async def classify(self) -> ClassifyResult:
        """POST /ckg/domains/classify -- backfill auto-taxonomy
        classification for any PROMOTED node with no domain assigned yet
        (covers nodes promoted before this feature existed, or whose
        classification failed during extraction). Idempotent: already
        classified nodes are left untouched."""
        resp = await self._client.post(self._url("/domains/classify"), params=self._params())
        resp.raise_for_status()
        data = resp.json()
        return ClassifyResult(
            classified=data.get("classified", 0),
            proposed_domains=data.get("proposed_domains", {}),
        )

    # ── sub-graphs ─────────────────────────────────────────────────────

    async def list_sub_graphs(self) -> list[SubGraphSummary]:
        """GET /ckg/sub-graphs -- every domain with at least one real node
        (itself or a descendant), with rollup node/edge counts and a
        computed health score. A domain with zero nodes never appears --
        there is no fabricated empty domain here."""
        resp = await self._client.get(self._url("/sub-graphs"), params=self._params())
        resp.raise_for_status()
        return [SubGraphSummary.from_dict(s) for s in resp.json().get("sub_graphs", [])]

    async def get_sub_graph(
        self,
        slug: str,
        *,
        limit: int = 200,
        offset: int = 0,
        confidence_min: float = 0.0,
        kind: str | None = None,
    ) -> SubGraph:
        """GET /ckg/sub-graphs/{slug} -- nodes + edges for one domain. If
        ``slug`` has registered children (e.g. "it"), this includes every
        descendant domain's nodes too (deduplicated by node id)."""
        params = self._params({
            "limit": limit, "offset": offset, "confidence_min": confidence_min,
        })
        if kind:
            params["kind"] = kind
        resp = await self._client.get(self._url(f"/sub-graphs/{slug}"), params=params)
        resp.raise_for_status()
        return SubGraph.from_dict(resp.json())

    # ── extraction (the "extract" step of connect -> vault -> extract ->
    # domain KG) ──────────────────────────────────────────────────────

    async def extract_documents(
        self,
        documents: list[dict[str, str]],
        *,
        engagement_id: str | None = None,
    ) -> ExtractionJob:
        """POST /ckg/extract -- run the N-pass extraction pipeline over raw
        documents directly (bypassing vault moderation). Each entry in
        *documents* must have ``filename`` and ``content`` keys. For
        vault-moderated content, prefer ``VaultClient.ingest(vault_id,
        target="kg")`` after ``approve()`` instead -- this method is for
        documents that don't need a moderation pass (e.g. a
        ``vouchstone harness scan`` result or a CI-generated report)."""
        if not documents:
            raise ValueError("at least one document is required")
        body: dict[str, Any] = {"documents": documents}
        if engagement_id is not None:
            body["engagement_id"] = engagement_id
        resp = await self._client.post(self._url("/extract"), params=self._params(), json=body)
        resp.raise_for_status()
        return ExtractionJob.from_dict(resp.json())

    async def get_extraction(self, job_id: str) -> ExtractionJob:
        """GET /ckg/extractions/{job_id} -- poll a single extraction job."""
        resp = await self._client.get(self._url(f"/extractions/{job_id}"), params=self._params())
        resp.raise_for_status()
        return ExtractionJob.from_dict(resp.json())

    async def wait_for_extraction(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 120.0,
    ) -> ExtractionJob:
        """Poll ``get_extraction`` until the job reaches a terminal status
        ("succeeded" or "failed") or ``timeout`` seconds elapse. Raises
        ``TimeoutError`` on timeout rather than returning a job that's
        still "running" -- a caller that ignores the return value should
        never mistake an in-progress job for a finished one."""
        deadline = time.monotonic() + timeout
        job = await self.get_extraction(job_id)
        while job.status not in _TERMINAL_JOB_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"extraction job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status!r})"
                )
            await asyncio.sleep(min(poll_interval, remaining))
            job = await self.get_extraction(job_id)
        return job

    # ── lifecycle ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> DomainClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
