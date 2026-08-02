"""JIRA ingester — fetches issues, sprints, epics and extracts KG entities."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

from .base import BaseIngester, RawEvent

logger = logging.getLogger("vouchstone.ingestion.jira")


class JiraIngester(BaseIngester):
    """Ingest JIRA project data into the Knowledge Graph.

    Connects via JIRA REST API (Basic Auth or OAuth). Fetches issues,
    comments, transitions, sprints, and epics. Extracts tasks, dependencies,
    blockers, sprint goals, and acceptance criteria.
    """

    source_name = "jira"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        projects: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")
        self._projects = projects or []
        cred = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {cred}",
            "Accept": "application/json",
        }

    async def connect(self) -> None:
        resp = await self._http.get(
            f"{self._base_url}/rest/api/3/myself",
            headers=self._headers,
        )
        if resp.status_code != 200:
            raise ConnectionError(f"JIRA auth failed: {resp.status_code}")
        data = resp.json()
        logger.info("Connected to JIRA as: %s", data.get("displayName"))

    async def _search_issues(self, jql: str, max_results: int = 100) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        start = 0
        while True:
            resp = await self._http.get(
                f"{self._base_url}/rest/api/3/search",
                params={
                    "jql": jql,
                    "startAt": start,
                    "maxResults": min(max_results - len(issues), 50),
                    "fields": "summary,description,status,assignee,priority,issuetype,"
                    "created,updated,labels,components,sprint,epic,issuelinks,"
                    "comment,resolution",
                },
                headers=self._headers,
            )
            data = resp.json()
            batch = data.get("issues", [])
            issues.extend(batch)
            if len(issues) >= data.get("total", 0) or len(issues) >= max_results:
                break
            start += len(batch)
        return issues

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        since_str = since.strftime("%Y-%m-%d")
        events: list[RawEvent] = []

        project_filter = ""
        if self._projects:
            proj_list = ", ".join(self._projects)
            project_filter = f"project in ({proj_list}) AND "

        jql = f"{project_filter}updated >= '{since_str}' ORDER BY updated DESC"
        issues = await self._search_issues(jql, max_results=500)

        for issue in issues:
            fields = issue.get("fields", {})
            key = issue.get("key", "")
            summary = fields.get("summary", "")
            desc = ""
            if fields.get("description"):
                desc_content = fields["description"]
                if isinstance(desc_content, dict):
                    desc = self._extract_adf_text(desc_content)
                else:
                    desc = str(desc_content)

            status = fields.get("status", {}).get("name", "Unknown")
            assignee = fields.get("assignee", {})
            assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
            priority = fields.get("priority", {}).get("name", "Medium")
            issue_type = fields.get("issuetype", {}).get("name", "Task")
            updated = fields.get("updated", "")

            ts = datetime.now(timezone.utc)
            if updated:
                try:
                    ts = datetime.fromisoformat(updated.replace("+0000", "+00:00"))
                except ValueError:
                    pass

            # Main issue event
            content = f"[{issue_type}] {key}: {summary}\nStatus: {status} | Priority: {priority} | Assignee: {assignee_name}"
            if desc:
                content += f"\n\n{desc[:500]}"

            links = fields.get("issuelinks", [])
            link_info = []
            for link in links:
                link_type = link.get("type", {}).get("name", "")
                if "inwardIssue" in link:
                    link_info.append(f"{link_type}: {link['inwardIssue'].get('key', '')}")
                if "outwardIssue" in link:
                    link_info.append(f"{link_type}: {link['outwardIssue'].get('key', '')}")

            events.append(
                RawEvent(
                    source="jira",
                    type="issue",
                    content=content,
                    timestamp=ts,
                    author=assignee_name,
                    metadata={
                        "key": key,
                        "status": status,
                        "priority": priority,
                        "issue_type": issue_type,
                        "labels": fields.get("labels", []),
                        "components": [
                            c.get("name", "") for c in fields.get("components", [])
                        ],
                        "links": link_info,
                        "resolution": fields.get("resolution", {}).get("name") if fields.get("resolution") else None,
                    },
                )
            )

            # Comments as separate events
            comments = fields.get("comment", {}).get("comments", [])
            for comment in comments:
                body = comment.get("body", "")
                if isinstance(body, dict):
                    body = self._extract_adf_text(body)

                c_author = comment.get("author", {}).get("displayName", "Unknown")
                c_created = comment.get("created", "")
                c_ts = ts
                if c_created:
                    try:
                        c_ts = datetime.fromisoformat(c_created.replace("+0000", "+00:00"))
                    except ValueError:
                        pass

                if c_ts >= since:
                    events.append(
                        RawEvent(
                            source="jira",
                            type="comment",
                            content=f"Comment on {key}: {body[:500]}",
                            timestamp=c_ts,
                            author=c_author,
                            metadata={"issue_key": key, "issue_summary": summary},
                        )
                    )

        logger.info("Fetched %d JIRA events from %d issues", len(events), len(issues))
        return events

    @staticmethod
    def _extract_adf_text(adf: dict[str, Any]) -> str:
        """Extract plain text from Atlassian Document Format."""
        parts: list[str] = []

        def walk(node: dict[str, Any] | list[Any]) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if isinstance(node, dict):
                if node.get("type") == "text":
                    parts.append(node.get("text", ""))
                for child in node.get("content", []):
                    walk(child)

        walk(adf)
        return " ".join(parts)
