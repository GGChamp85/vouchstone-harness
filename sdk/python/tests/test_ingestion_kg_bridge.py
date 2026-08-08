"""B1 -- the unified KG schema across ingestion and artifacts.

Covers: the EXTRACTION_STRATEGIES registry actually driving
BaseIngester.extract_entities (it was an empty, consumer-less registry
before), the zero-LLM deterministic strategy, the ingestion->canonical
Entity conversion, and build_source_artifact producing the same signed,
verifiable artifact format as a codebase build -- from a live-source
ingester, fully offline.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vouchstone_sdk.ingestion.base import (
    BaseIngester,
    Entity,
    EntityType,
    RawEvent,
    to_canonical_entity,
)
from vouchstone_sdk.kg import build_source_artifact, verify_artifact
from vouchstone_sdk.plugins import EXTRACTION_STRATEGIES


class FakeSlackIngester(BaseIngester):
    """Real BaseIngester subclass with canned events -- exercises the
    genuine strategy/build_graph/artifact path with no network."""

    source_name = "slack"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return [
            RawEvent(source="slack", type="decision", author="Jane",
                     timestamp=ts, content="Use DECIMAL, not FLOAT, for finance columns"),
            RawEvent(source="slack", type="message", author="Mike",
                     timestamp=ts, content="Deploy window is Saturday 2am"),
        ]


def test_builtin_strategies_registered():
    names = EXTRACTION_STRATEGIES.names()
    assert "llm" in names and "deterministic" in names


@pytest.mark.asyncio
async def test_deterministic_strategy_extracts_without_llm():
    ingester = FakeSlackIngester(extraction_strategy="deterministic")
    events = await ingester.fetch_raw(datetime.now(timezone.utc))
    entities = await ingester.extract_entities(events)
    await ingester.close()

    by_type: dict[str, list[Entity]] = {}
    for e in entities:
        by_type.setdefault(e.type.value, []).append(e)

    # "decision" maps into the taxonomy; "message" falls back to document.
    assert len(by_type["decision"]) == 1
    assert by_type["decision"][0].name.startswith("Use DECIMAL")
    assert len(by_type["document"]) == 1
    # one PERSON per distinct author, confidence 1.0 (nothing inferred)
    assert {p.name for p in by_type["person"]} == {"Jane", "Mike"}
    assert all(e.confidence == 1.0 for e in entities)


@pytest.mark.asyncio
async def test_unknown_strategy_raises_with_available_names():
    ingester = FakeSlackIngester(extraction_strategy="nope")
    with pytest.raises(KeyError) as err:
        await ingester.extract_entities([])
    assert "nope" in str(err.value)
    await ingester.close()


def test_to_canonical_entity_maps_schema():
    ing = Entity(
        id="ent-1", type=EntityType.DECISION, name="Use DECIMAL",
        description="finance columns", source="slack", source_event_id="evt-9",
        confidence=0.9, attributes={"channel": "#eng"},
    )
    canonical = to_canonical_entity(ing)
    assert canonical.id == "ent-1"
    assert canonical.entity_type == "decision"
    assert canonical.entity_key == "slack:Use DECIMAL"
    assert canonical.confidence == 0.9
    assert canonical.attributes["domains"] == ["slack"]
    assert canonical.attributes["channel"] == "#eng"
    assert canonical.attributes["source_event_id"] == "evt-9"


@pytest.mark.asyncio
async def test_build_source_artifact_signed_and_verifiable():
    ingester = FakeSlackIngester(extraction_strategy="deterministic")
    artifact = await build_source_artifact(
        ingester, datetime.now(timezone.utc),
    )
    await ingester.close()

    assert ingester.connected
    assert artifact.root_label == "slack"
    # every raw event's content hash is in the signed manifest
    assert len(artifact.source_hashes) == 2
    assert all(k.startswith("slack/evt-") for k in artifact.source_hashes)
    assert verify_artifact(artifact).valid

    # entities landed in the canonical schema with source-domain tagging
    decisions = artifact.graph.entities_by_type("decision")
    assert len(decisions) == 1
    assert decisions[0].attributes["domains"] == ["slack"]
