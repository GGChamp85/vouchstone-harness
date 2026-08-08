"""Confluence ingester — fetches pages, comments and extracts KG entities."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

from .base import BaseIngester, RawEvent

logger = logging.getLogger("vouchstone.ingestion.confluence")


class ConfluenceIngester(BaseIngester):
    """Ingest Confluence wiki data into the Knowledge Graph.

    Connects via Confluence REST API. Fetches pages, comments,
    attachments, and page tree. Extracts architecture decisions,
    runbooks, API docs, and onboarding guides. Chunks large
    documents for vector embedding.
    """

    source_name = "confluence"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        spaces: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")
        self._spaces = spaces or []
        cred = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {cred}",
            "Accept": "application/json",
        }

    async def connect(self) -> None:
        resp = await self._http.get(
            f"{self._base_url}/wiki/rest/api/user/current",
            headers=self._headers,
        )
        if resp.status_code != 200:
            raise ConnectionError(f"Confluence auth failed: {resp.status_code}")
        data = resp.json()
        logger.info("Connected to Confluence as: %s", data.get("displayName"))

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        events: list[RawEvent] = []

        for space_key in self._spaces or await self._list_spaces():
            start = 0
            while True:
                params: dict[str, Any] = {
                    "spaceKey": space_key,
                    "expand": "body.storage,version,ancestors,metadata.labels",
                    "limit": 25,
                    "start": start,
                    "orderby": "modified desc",
                }
                resp = await self._http.get(
                    f"{self._base_url}/wiki/rest/api/content",
                    params=params,
                    headers=self._headers,
                )
                if resp.status_code != 200:
                    break

                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break

                for page in results:
                    version = page.get("version", {})
                    modified = version.get("when", "")
                    page_ts = datetime.now(timezone.utc)
                    if modified:
                        try:
                            page_ts = datetime.fromisoformat(
                                modified.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass

                    if page_ts < since:
                        continue

                    title = page.get("title", "")
                    body_html = (
                        page.get("body", {}).get("storage", {}).get("value", "")
                    )
                    body_text = self._html_to_text(body_html)
                    author = version.get("by", {}).get("displayName", "Unknown")
                    labels = [
                        label.get("name", "")
                        for label in page.get("metadata", {})
                        .get("labels", {})
                        .get("results", [])
                    ]
                    ancestors = [
                        a.get("title", "")
                        for a in page.get("ancestors", [])
                    ]

                    # Classify document type
                    doc_type = "document"
                    title_lower = title.lower()
                    if any(kw in title_lower for kw in ["adr", "decision", "rfc"]):
                        doc_type = "architecture_decision"
                    elif any(kw in title_lower for kw in ["runbook", "playbook", "sop"]):
                        doc_type = "runbook"
                    elif any(kw in title_lower for kw in ["api", "endpoint", "swagger"]):
                        doc_type = "api_doc"
                    elif any(kw in title_lower for kw in ["onboard", "getting started", "setup"]):
                        doc_type = "onboarding"
                    elif any(kw in title_lower for kw in ["meeting", "minutes", "standup"]):
                        doc_type = "meeting_notes"

                    # Chunk large documents
                    chunks = self._chunk_text(body_text, max_chars=2000)
                    for i, chunk in enumerate(chunks):
                        events.append(
                            RawEvent(
                                source="confluence",
                                type=doc_type,
                                content=f"{title}\n\n{chunk}" if i == 0 else f"{title} (part {i + 1})\n\n{chunk}",
                                timestamp=page_ts,
                                author=author,
                                metadata={
                                    "page_id": page.get("id", ""),
                                    "space": space_key,
                                    "title": title,
                                    "labels": labels,
                                    "ancestors": ancestors,
                                    "chunk_index": i,
                                    "total_chunks": len(chunks),
                                    "version": version.get("number", 1),
                                },
                            )
                        )

                size = data.get("size", 0)
                if len(results) < 25 or start + size >= data.get("totalSize", 0):
                    break
                start += len(results)

        logger.info("Fetched %d Confluence events", len(events))
        return events

    async def _list_spaces(self) -> list[str]:
        resp = await self._http.get(
            f"{self._base_url}/wiki/rest/api/space",
            params={"limit": 100, "type": "global"},
            headers=self._headers,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [s.get("key", "") for s in data.get("results", [])]

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Strip HTML tags to extract plain text."""
        import re

        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 2000) -> list[str]:
        """Split text into chunks at paragraph boundaries."""
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        paragraphs = text.split("\n\n")
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            if current_len + len(para) > max_chars and current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            current.append(para)
            current_len += len(para) + 2

        if current:
            chunks.append("\n\n".join(current))

        return chunks
