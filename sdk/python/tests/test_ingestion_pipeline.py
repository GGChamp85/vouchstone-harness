"""IngestionPipeline -- scheduling regression + cross-source dedup.

The scheduled-sync loop previously computed its lookback window with
``datetime.replace(hour=hour - since_hours)``, which raises
``ValueError: hour must be in 0..23`` whenever the wall-clock hour is
smaller than ``since_hours`` -- i.e. always, for the default 24 -- so the
very first scheduled firing crashed. The regression test drives one real
iteration of the loop (with sleep patched to fire immediately) and asserts
the computed ``since`` is a genuine timedelta lookback.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from vouchstone_sdk.ingestion.base import Entity, EntityType, Relationship
from vouchstone_sdk.ingestion.pipeline import IngestionPipeline


@pytest.mark.asyncio
async def test_scheduled_sync_computes_since_with_timedelta(monkeypatch):
    pipeline = IngestionPipeline()
    captured: dict[str, datetime] = {}
    fired = asyncio.Event()

    async def fake_sync_all(since: datetime):
        captured["since"] = since
        fired.set()
        return {}

    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds: float) -> None:
        # Must still yield to the event loop -- a bare `return` makes the
        # scheduler's while-True a busy loop that starves wait_for below.
        await real_sleep(0)

    monkeypatch.setattr(pipeline, "sync_all", fake_sync_all)
    monkeypatch.setattr(
        "vouchstone_sdk.ingestion.pipeline.asyncio.sleep", instant_sleep
    )

    pipeline.schedule("1h", since_hours=24)
    try:
        await asyncio.wait_for(fired.wait(), timeout=2)
    finally:
        pipeline.stop_schedule()

    since = captured["since"]
    expected = datetime.now(timezone.utc) - timedelta(hours=24)
    # Within a minute of exactly-24h-ago, and in the past -- the .replace()
    # bug either crashed outright or produced a same-day wrong timestamp.
    assert abs((since - expected).total_seconds()) < 60
    assert since < datetime.now(timezone.utc)


def test_deduplicate_entities_latest_timestamp_wins():
    older = Entity(
        id="e1", type=EntityType.SYSTEM, name="Billing API",
        description="old copy",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = Entity(
        id="e2", type=EntityType.SYSTEM, name="billing api",  # same dedup key
        description="new copy",
        updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    result = IngestionPipeline.deduplicate_entities([older, newer])
    assert len(result) == 1
    assert result[0].description == "new copy"


def test_deduplicate_relationships_highest_confidence_wins():
    low = Relationship(source_entity_id="a", target_entity_id="b",
                       type="DEPENDS_ON", confidence=0.4)
    high = Relationship(source_entity_id="a", target_entity_id="b",
                        type="DEPENDS_ON", confidence=0.9)
    result = IngestionPipeline.deduplicate_relationships([low, high])
    assert len(result) == 1
    assert result[0].confidence == 0.9
