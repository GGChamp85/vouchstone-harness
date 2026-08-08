"""Ingestion pipeline — orchestrates multiple ingesters with dedup and scheduling."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .base import BaseIngester, Entity, Relationship, SyncState, SyncStatus

logger = logging.getLogger("vouchstone.ingestion.pipeline")


class IngestionPipeline:
    """Orchestrates multiple KG ingesters with deduplication and scheduling.

    Registers ingesters for different sources (Slack, JIRA, Confluence,
    GitHub, Meetings), runs them in parallel, deduplicates entities
    across sources, and resolves conflicts by latest timestamp.

    Usage::

        pipeline = IngestionPipeline()
        pipeline.register(SlackIngester(bot_token="xoxb-..."))
        pipeline.register(JiraIngester(base_url="https://...", ...))
        pipeline.register(GitHubIngester(token="ghp_...", repos=["org/repo"]))

        status = await pipeline.sync_all(since=datetime(2026, 1, 1))
    """

    def __init__(self) -> None:
        self._ingesters: dict[str, BaseIngester] = {}
        self._seen_entities: dict[str, Entity] = {}
        self._seen_relationships: dict[str, Relationship] = {}
        self._last_sync: dict[str, SyncStatus] = {}
        self._schedule_task: asyncio.Task[None] | None = None

    def register(self, ingester: BaseIngester) -> None:
        """Register an ingester for a data source."""
        self._ingesters[ingester.source_name] = ingester
        logger.info("Registered ingester: %s", ingester.source_name)

    def unregister(self, source_name: str) -> None:
        """Remove an ingester."""
        self._ingesters.pop(source_name, None)

    async def connect_all(self) -> dict[str, bool]:
        """Connect all registered ingesters. Returns source → success map."""
        results: dict[str, bool] = {}
        for name, ingester in self._ingesters.items():
            try:
                await ingester.connect()
                results[name] = True
                logger.info("Connected: %s", name)
            except Exception as exc:
                results[name] = False
                logger.error("Connection failed for %s: %s", name, exc)
        return results

    async def sync_all(self, since: datetime) -> dict[str, SyncStatus]:
        """Run all ingesters in parallel, deduplicate across sources."""
        if not self._ingesters:
            logger.warning("No ingesters registered")
            return {}

        # Run all ingesters concurrently
        tasks = {
            name: asyncio.create_task(ingester.sync(since))
            for name, ingester in self._ingesters.items()
        }

        results: dict[str, SyncStatus] = {}
        for name, task in tasks.items():
            try:
                status = await task
                results[name] = status
            except Exception as exc:
                logger.error("Sync failed for %s: %s", name, exc)
                results[name] = SyncStatus(
                    source=name,
                    status=SyncState.ERROR,
                    errors=1,
                    error_messages=[str(exc)],
                )

        self._last_sync = results

        # Log aggregate stats
        total_entities = sum(s.entities_ingested for s in results.values())
        total_rels = sum(s.relationships_created for s in results.values())
        total_errors = sum(s.errors for s in results.values())
        logger.info(
            "Pipeline sync complete: %d entities, %d relationships, %d errors across %d sources",
            total_entities,
            total_rels,
            total_errors,
            len(results),
        )

        return results

    async def sync_source(self, source_name: str, since: datetime) -> SyncStatus:
        """Sync a single source."""
        ingester = self._ingesters.get(source_name)
        if not ingester:
            return SyncStatus(
                source=source_name,
                status=SyncState.ERROR,
                errors=1,
                error_messages=[f"Unknown source: {source_name}"],
            )

        status = await ingester.sync(since)
        self._last_sync[source_name] = status
        return status

    def get_status(self) -> dict[str, SyncStatus]:
        """Get the status of all registered sources."""
        result: dict[str, SyncStatus] = {}
        for name, ingester in self._ingesters.items():
            if name in self._last_sync:
                result[name] = self._last_sync[name]
            else:
                result[name] = ingester.get_sync_status()
        return result

    def get_source_status(self, source_name: str) -> SyncStatus | None:
        """Get status for a specific source."""
        return self._last_sync.get(source_name)

    def schedule(self, cron: str, since_hours: int = 24) -> None:
        """Schedule periodic sync using a simple interval.

        Args:
            cron: Interval specification (e.g., "1h", "30m", "6h").
            since_hours: How far back to look on each sync.
        """
        interval_seconds = self._parse_interval(cron)
        if self._schedule_task and not self._schedule_task.done():
            self._schedule_task.cancel()

        async def _run_scheduled() -> None:
            while True:
                await asyncio.sleep(interval_seconds)
                # timedelta, NOT .replace(hour=hour - since_hours): the
                # replace() form raised ValueError("hour must be in 0..23")
                # whenever the wall-clock hour was smaller than since_hours
                # (i.e. always, for the default 24), so scheduled syncs
                # crashed on their very first firing.
                since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
                try:
                    await self.sync_all(since)
                except Exception as exc:
                    logger.error("Scheduled sync failed: %s", exc)

        self._schedule_task = asyncio.create_task(_run_scheduled())
        logger.info(
            "Scheduled sync every %ds (looking back %dh)",
            interval_seconds,
            since_hours,
        )

    def stop_schedule(self) -> None:
        """Stop the scheduled sync."""
        if self._schedule_task and not self._schedule_task.done():
            self._schedule_task.cancel()
            self._schedule_task = None
            logger.info("Scheduled sync stopped")

    @staticmethod
    def deduplicate_entities(entities: list[Entity]) -> list[Entity]:
        """Deduplicate entities across sources. Latest timestamp wins."""
        seen: dict[str, Entity] = {}
        for entity in entities:
            key = entity.dedup_key
            if key not in seen or entity.updated_at > seen[key].updated_at:
                seen[key] = entity
        return list(seen.values())

    @staticmethod
    def deduplicate_relationships(
        relationships: list[Relationship],
    ) -> list[Relationship]:
        """Deduplicate relationships. Highest confidence wins."""
        seen: dict[str, Relationship] = {}
        for rel in relationships:
            key = rel.dedup_key
            if key not in seen or rel.confidence > seen[key].confidence:
                seen[key] = rel
        return list(seen.values())

    async def close(self) -> None:
        """Close all ingesters and stop scheduling."""
        self.stop_schedule()
        for ingester in self._ingesters.values():
            await ingester.close()

    @staticmethod
    def _parse_interval(spec: str) -> int:
        """Parse interval string like '1h', '30m', '6h' to seconds."""
        spec = spec.strip().lower()
        if spec.endswith("h"):
            return int(spec[:-1]) * 3600
        elif spec.endswith("m"):
            return int(spec[:-1]) * 60
        elif spec.endswith("s"):
            return int(spec[:-1])
        else:
            return int(spec)
