"""Knowledge Graph ingestion pipeline for Vouchstone.

Ingest data from enterprise tools (Slack, JIRA, Confluence, GitHub,
Google Meet/Zoom/Teams) into the Knowledge Graph. All processing
runs in the client's VPC — no data leaves the data plane.

Usage::

    from vouchstone_sdk.ingestion import (
        IngestionPipeline,
        SlackIngester,
        JiraIngester,
        ConfluenceIngester,
        GitHubIngester,
        MeetingIngester,
    )

    pipeline = IngestionPipeline()
    pipeline.register(SlackIngester(bot_token="xoxb-..."))
    pipeline.register(JiraIngester(base_url="https://...", email="...", api_token="..."))
    pipeline.register(GitHubIngester(token="ghp_...", repos=["org/repo"]))
    pipeline.register(ConfluenceIngester(base_url="https://...", email="...", api_token="..."))
    pipeline.register(MeetingIngester(google_credentials={...}))

    await pipeline.connect_all()
    status = await pipeline.sync_all(since=datetime(2026, 1, 1))
"""

from .base import (
    BaseIngester,
    Entity,
    EntityType,
    RawEvent,
    Relationship,
    SyncState,
    SyncStatus,
)
from .confluence import ConfluenceIngester
from .github import GitHubIngester
from .jira import JiraIngester
from .meetings import MeetingIngester
from .pipeline import IngestionPipeline
from .slack import SlackIngester

__all__ = [
    "BaseIngester",
    "ConfluenceIngester",
    "Entity",
    "EntityType",
    "GitHubIngester",
    "IngestionPipeline",
    "JiraIngester",
    "MeetingIngester",
    "RawEvent",
    "Relationship",
    "SlackIngester",
    "SyncState",
    "SyncStatus",
]
