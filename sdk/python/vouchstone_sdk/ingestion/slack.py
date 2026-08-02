"""Slack ingester — fetches messages, threads, reactions and extracts KG entities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import BaseIngester, Entity, EntityType, RawEvent

logger = logging.getLogger("vouchstone.ingestion.slack")


class SlackIngester(BaseIngester):
    """Ingest Slack workspace data into the Knowledge Graph.

    Connects via Slack Web API (Bot token). Fetches messages, threads,
    reactions, file shares, and channel topics. Uses LLM to classify
    message intent and extract structured entities.
    """

    source_name = "slack"

    def __init__(
        self,
        *,
        bot_token: str,
        channels: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bot_token = bot_token
        self._channels = channels
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        }

    async def connect(self) -> None:
        resp = await self._http.post(
            "https://slack.com/api/auth.test",
            headers=self._headers,
        )
        data = resp.json()
        if not data.get("ok"):
            raise ConnectionError(f"Slack auth failed: {data.get('error')}")
        logger.info("Connected to Slack workspace: %s", data.get("team"))

    async def _list_channels(self) -> list[dict[str, Any]]:
        if self._channels:
            channels = []
            for ch_id in self._channels:
                resp = await self._http.get(
                    "https://slack.com/api/conversations.info",
                    params={"channel": ch_id},
                    headers=self._headers,
                )
                data = resp.json()
                if data.get("ok"):
                    channels.append(data["channel"])
            return channels

        channels = []
        cursor = None
        while True:
            params: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = await self._http.get(
                "https://slack.com/api/conversations.list",
                params=params,
                headers=self._headers,
            )
            data = resp.json()
            if not data.get("ok"):
                break
            channels.extend(data.get("channels", []))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        channels = await self._list_channels()
        oldest = str(since.timestamp())
        events: list[RawEvent] = []

        for channel in channels:
            ch_id = channel["id"]
            ch_name = channel.get("name", ch_id)
            cursor = None

            while True:
                params: dict[str, Any] = {
                    "channel": ch_id,
                    "oldest": oldest,
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await self._http.get(
                    "https://slack.com/api/conversations.history",
                    params=params,
                    headers=self._headers,
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.warning("Failed to fetch #%s: %s", ch_name, data.get("error"))
                    break

                for msg in data.get("messages", []):
                    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                        continue

                    ts = float(msg.get("ts", "0"))
                    reactions = msg.get("reactions", [])
                    reaction_count = sum(r.get("count", 0) for r in reactions)
                    thread_count = msg.get("reply_count", 0)

                    event_type = "message"
                    if reaction_count >= 3:
                        event_type = "decision_signal"
                    elif thread_count >= 5:
                        event_type = "discussion"

                    events.append(
                        RawEvent(
                            source="slack",
                            type=event_type,
                            content=msg.get("text", ""),
                            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                            author=msg.get("user", "unknown"),
                            metadata={
                                "channel": ch_name,
                                "channel_id": ch_id,
                                "thread_ts": msg.get("thread_ts"),
                                "reactions": reactions,
                                "reaction_count": reaction_count,
                                "reply_count": thread_count,
                                "files": [
                                    f.get("name") for f in msg.get("files", [])
                                ],
                            },
                        )
                    )

                    # Fetch thread replies for high-signal messages
                    if thread_count > 0 and msg.get("thread_ts") == msg.get("ts"):
                        thread_resp = await self._http.get(
                            "https://slack.com/api/conversations.replies",
                            params={
                                "channel": ch_id,
                                "ts": msg["ts"],
                                "oldest": oldest,
                                "limit": 100,
                            },
                            headers=self._headers,
                        )
                        thread_data = thread_resp.json()
                        for reply in thread_data.get("messages", [])[1:]:
                            rts = float(reply.get("ts", "0"))
                            events.append(
                                RawEvent(
                                    source="slack",
                                    type="thread_reply",
                                    content=reply.get("text", ""),
                                    timestamp=datetime.fromtimestamp(rts, tz=timezone.utc),
                                    author=reply.get("user", "unknown"),
                                    metadata={
                                        "channel": ch_name,
                                        "thread_ts": msg["ts"],
                                        "parent_text": msg.get("text", "")[:200],
                                    },
                                )
                            )

                cursor = data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        logger.info("Fetched %d Slack events across %d channels", len(events), len(channels))
        return events

    async def extract_entities(self, events: list[RawEvent]) -> list[Entity]:
        """Override to add Slack-specific classification before LLM extraction."""
        # Pre-classify high-signal events
        for event in events:
            rc = event.metadata.get("reaction_count", 0)
            if rc >= 5:
                event.type = "strong_decision"
            elif event.metadata.get("reply_count", 0) >= 10:
                event.type = "major_discussion"

            # Detect action items by pattern
            text_lower = event.content.lower()
            if any(kw in text_lower for kw in ["action item", "todo", "assigned to", "please", "can you", "need to"]):
                event.type = "action_signal"

        return await super().extract_entities(events)
