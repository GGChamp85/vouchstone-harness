"""GitHub ingester — fetches PRs, commits, issues and extracts KG entities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import BaseIngester, RawEvent

logger = logging.getLogger("vouchstone.ingestion.github")


class GitHubIngester(BaseIngester):
    """Ingest GitHub repository data into the Knowledge Graph.

    Connects via GitHub REST API. Fetches PRs, commits, reviews,
    issues, and code files. Extracts code changes, review decisions,
    CI/CD events, and dependency relationships.
    """

    source_name = "github"

    def __init__(
        self,
        *,
        token: str,
        repos: list[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._repos = repos
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def connect(self) -> None:
        resp = await self._http.get(
            "https://api.github.com/user",
            headers=self._headers,
        )
        if resp.status_code != 200:
            raise ConnectionError(f"GitHub auth failed: {resp.status_code}")
        data = resp.json()
        logger.info("Connected to GitHub as: %s", data.get("login"))

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        events: list[RawEvent] = []
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        for repo in self._repos:
            # Fetch PRs
            pr_events = await self._fetch_prs(repo, since_str)
            events.extend(pr_events)

            # Fetch commits
            commit_events = await self._fetch_commits(repo, since_str)
            events.extend(commit_events)

            # Fetch issues
            issue_events = await self._fetch_issues(repo, since_str)
            events.extend(issue_events)

        logger.info("Fetched %d GitHub events from %d repos", len(events), len(self._repos))
        return events

    async def _fetch_prs(self, repo: str, since: str) -> list[RawEvent]:
        events: list[RawEvent] = []
        page = 1

        while True:
            resp = await self._http.get(
                f"https://api.github.com/repos/{repo}/pulls",
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 50,
                    "page": page,
                },
                headers=self._headers,
            )
            if resp.status_code != 200:
                break

            prs = resp.json()
            if not prs:
                break

            for pr in prs:
                updated = pr.get("updated_at", "")
                if updated < since:
                    return events

                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                author = pr.get("user", {}).get("login", "unknown")
                title = pr.get("title", "")
                body = pr.get("body", "") or ""
                state = pr.get("state", "open")
                merged = pr.get("merged_at") is not None

                files_resp = await self._http.get(
                    f"https://api.github.com/repos/{repo}/pulls/{pr['number']}/files",
                    params={"per_page": 100},
                    headers=self._headers,
                )
                files = files_resp.json() if files_resp.status_code == 200 else []
                changed_files = [f.get("filename", "") for f in files[:20]]
                additions = sum(f.get("additions", 0) for f in files)
                deletions = sum(f.get("deletions", 0) for f in files)

                events.append(
                    RawEvent(
                        source="github",
                        type="pull_request",
                        content=f"PR #{pr['number']}: {title}\n\n{body[:500]}",
                        timestamp=ts,
                        author=author,
                        metadata={
                            "repo": repo,
                            "number": pr["number"],
                            "state": state,
                            "merged": merged,
                            "files_changed": changed_files,
                            "additions": additions,
                            "deletions": deletions,
                            "labels": [label.get("name", "") for label in pr.get("labels", [])],
                            "reviewers": [
                                r.get("login", "")
                                for r in pr.get("requested_reviewers", [])
                            ],
                        },
                    )
                )

                # Fetch reviews
                reviews_resp = await self._http.get(
                    f"https://api.github.com/repos/{repo}/pulls/{pr['number']}/reviews",
                    headers=self._headers,
                )
                if reviews_resp.status_code == 200:
                    for review in reviews_resp.json():
                        r_state = review.get("state", "")
                        if r_state in ("APPROVED", "CHANGES_REQUESTED"):
                            r_ts_str = review.get("submitted_at", updated)
                            r_ts = datetime.fromisoformat(r_ts_str.replace("Z", "+00:00"))
                            events.append(
                                RawEvent(
                                    source="github",
                                    type="review",
                                    content=f"Review on PR #{pr['number']}: {r_state}. {review.get('body', '')[:300]}",
                                    timestamp=r_ts,
                                    author=review.get("user", {}).get("login", "unknown"),
                                    metadata={
                                        "repo": repo,
                                        "pr_number": pr["number"],
                                        "pr_title": title,
                                        "state": r_state,
                                    },
                                )
                            )

            page += 1
            if len(prs) < 50:
                break

        return events

    async def _fetch_commits(self, repo: str, since: str) -> list[RawEvent]:
        events: list[RawEvent] = []

        resp = await self._http.get(
            f"https://api.github.com/repos/{repo}/commits",
            params={"since": since, "per_page": 100},
            headers=self._headers,
        )
        if resp.status_code != 200:
            return events

        for commit in resp.json():
            c = commit.get("commit", {})
            author = c.get("author", {})
            ts_str = author.get("date", "")
            ts = datetime.now(timezone.utc)
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

            message = c.get("message", "")
            events.append(
                RawEvent(
                    source="github",
                    type="commit",
                    content=f"Commit: {message}",
                    timestamp=ts,
                    author=author.get("name", "unknown"),
                    metadata={
                        "repo": repo,
                        "sha": commit.get("sha", "")[:8],
                        "stats": commit.get("stats", {}),
                    },
                )
            )

        return events

    async def _fetch_issues(self, repo: str, since: str) -> list[RawEvent]:
        events: list[RawEvent] = []

        resp = await self._http.get(
            f"https://api.github.com/repos/{repo}/issues",
            params={
                "state": "all",
                "since": since,
                "per_page": 50,
                "sort": "updated",
            },
            headers=self._headers,
        )
        if resp.status_code != 200:
            return events

        for issue in resp.json():
            if issue.get("pull_request"):
                continue

            updated = issue.get("updated_at", "")
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            author = issue.get("user", {}).get("login", "unknown")

            events.append(
                RawEvent(
                    source="github",
                    type="issue",
                    content=f"Issue #{issue['number']}: {issue.get('title', '')}\n\n{(issue.get('body', '') or '')[:500]}",
                    timestamp=ts,
                    author=author,
                    metadata={
                        "repo": repo,
                        "number": issue["number"],
                        "state": issue.get("state", "open"),
                        "labels": [label.get("name", "") for label in issue.get("labels", [])],
                        "assignees": [
                            a.get("login", "")
                            for a in issue.get("assignees", [])
                        ],
                    },
                )
            )

        return events
