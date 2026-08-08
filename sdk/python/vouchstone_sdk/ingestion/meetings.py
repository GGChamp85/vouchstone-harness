"""Meeting ingester — processes transcripts from Google Meet, Zoom, Teams."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .base import BaseIngester, Entity, EntityType, RawEvent

logger = logging.getLogger("vouchstone.ingestion.meetings")


class MeetingIngester(BaseIngester):
    """Ingest meeting transcripts into the Knowledge Graph.

    Processes transcripts from Google Meet (via Google Drive API),
    Zoom (via recording API), and Microsoft Teams (via Graph API).
    Extracts decisions, action items, risks, questions, and follow-ups
    using LLM-powered analysis.
    """

    source_name = "meetings"

    def __init__(
        self,
        *,
        google_credentials: dict[str, Any] | None = None,
        zoom_token: str | None = None,
        teams_token: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._google_creds = google_credentials
        self._zoom_token = zoom_token
        self._teams_token = teams_token

    async def connect(self) -> None:
        connected = []
        if self._google_creds:
            connected.append("Google Meet")
        if self._zoom_token:
            resp = await self._http.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {self._zoom_token}"},
            )
            if resp.status_code == 200:
                connected.append("Zoom")
        if self._teams_token:
            resp = await self._http.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {self._teams_token}"},
            )
            if resp.status_code == 200:
                connected.append("Teams")

        if not connected:
            raise ConnectionError("No meeting platform credentials provided")
        logger.info("Connected to meeting platforms: %s", ", ".join(connected))

    async def fetch_raw(self, since: datetime) -> list[RawEvent]:
        events: list[RawEvent] = []

        if self._google_creds:
            events.extend(await self._fetch_google_meet(since))
        if self._zoom_token:
            events.extend(await self._fetch_zoom(since))
        if self._teams_token:
            events.extend(await self._fetch_teams(since))

        logger.info("Fetched %d meeting events", len(events))
        return events

    async def _fetch_google_meet(self, since: datetime) -> list[RawEvent]:
        """Fetch Google Meet transcripts via Google Drive API."""
        events: list[RawEvent] = []
        # Google Meet stores transcripts as Google Docs in Drive
        # Search for transcript files created after `since`
        access_token = self._google_creds.get("access_token", "") if self._google_creds else ""
        if not access_token:
            return events

        since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
        resp = await self._http.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": f"name contains 'Transcript' and mimeType='application/vnd.google-apps.document' and modifiedTime > '{since_str}'",
                "fields": "files(id,name,createdTime,modifiedTime)",
                "pageSize": 50,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return events

        for file in resp.json().get("files", []):
            # Export transcript as plain text
            export_resp = await self._http.get(
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                params={"mimeType": "text/plain"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if export_resp.status_code != 200:
                continue

            transcript = export_resp.text
            ts_str = file.get("createdTime", "")
            ts = datetime.now(timezone.utc)
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            events.append(
                RawEvent(
                    source="meetings",
                    type="transcript",
                    content=transcript,
                    timestamp=ts,
                    author="Google Meet",
                    metadata={
                        "platform": "google_meet",
                        "file_id": file["id"],
                        "title": file.get("name", ""),
                    },
                )
            )

        return events

    async def _fetch_zoom(self, since: datetime) -> list[RawEvent]:
        """Fetch Zoom cloud recording transcripts."""
        events: list[RawEvent] = []
        since_str = since.strftime("%Y-%m-%d")

        resp = await self._http.get(
            "https://api.zoom.us/v2/users/me/recordings",
            params={"from": since_str, "page_size": 30},
            headers={"Authorization": f"Bearer {self._zoom_token}"},
        )
        if resp.status_code != 200:
            return events

        for meeting in resp.json().get("meetings", []):
            for recording in meeting.get("recording_files", []):
                if recording.get("recording_type") != "audio_transcript":
                    continue

                download_url = recording.get("download_url", "")
                if not download_url:
                    continue

                transcript_resp = await self._http.get(
                    download_url,
                    headers={"Authorization": f"Bearer {self._zoom_token}"},
                )
                if transcript_resp.status_code != 200:
                    continue

                ts_str = meeting.get("start_time", "")
                ts = datetime.now(timezone.utc)
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                events.append(
                    RawEvent(
                        source="meetings",
                        type="transcript",
                        content=transcript_resp.text,
                        timestamp=ts,
                        author="Zoom",
                        metadata={
                            "platform": "zoom",
                            "meeting_id": meeting.get("id", ""),
                            "title": meeting.get("topic", ""),
                            "duration": meeting.get("duration", 0),
                            "participants": meeting.get("total_size", 0),
                        },
                    )
                )

        return events

    async def _fetch_teams(self, since: datetime) -> list[RawEvent]:
        """Fetch Microsoft Teams meeting transcripts."""
        events: list[RawEvent] = []

        resp = await self._http.get(
            "https://graph.microsoft.com/v1.0/me/onlineMeetings",
            params={
                "$filter": f"startDateTime ge {since.isoformat()}",
                "$top": 50,
                "$orderby": "startDateTime desc",
            },
            headers={"Authorization": f"Bearer {self._teams_token}"},
        )
        if resp.status_code != 200:
            return events

        for meeting in resp.json().get("value", []):
            meeting_id = meeting.get("id", "")
            # Fetch transcript
            t_resp = await self._http.get(
                f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{meeting_id}/transcripts",
                headers={"Authorization": f"Bearer {self._teams_token}"},
            )
            if t_resp.status_code != 200:
                continue

            for transcript in t_resp.json().get("value", []):
                t_id = transcript.get("id", "")
                content_resp = await self._http.get(
                    f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{meeting_id}/transcripts/{t_id}/content",
                    params={"$format": "text/plain"},
                    headers={"Authorization": f"Bearer {self._teams_token}"},
                )
                if content_resp.status_code != 200:
                    continue

                ts_str = meeting.get("startDateTime", "")
                ts = datetime.now(timezone.utc)
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                events.append(
                    RawEvent(
                        source="meetings",
                        type="transcript",
                        content=content_resp.text,
                        timestamp=ts,
                        author="Teams",
                        metadata={
                            "platform": "teams",
                            "meeting_id": meeting_id,
                            "title": meeting.get("subject", ""),
                        },
                    )
                )

        return events

    async def extract_entities(self, events: list[RawEvent]) -> list[Entity]:
        """Override to use meeting-specific LLM extraction."""
        if not events:
            return []

        all_entities: list[Entity] = []

        for event in events:
            if event.type != "transcript":
                continue

            # Use LLM to extract structured meeting data
            try:
                resp = await self._openai_client.chat.completions.create(
                    model=self._openai_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a meeting analyst. Extract structured data from the transcript.\n"
                                "Return a JSON object with:\n"
                                '- "summary": one-paragraph meeting summary\n'
                                '- "decisions": array of {decision, rationale, decided_by}\n'
                                '- "action_items": array of {task, assignee, due_date, priority}\n'
                                '- "risks": array of {risk, severity, owner}\n'
                                '- "questions": array of {question, asked_by, status: open|resolved}\n'
                                '- "follow_ups": array of {topic, responsible, deadline}\n'
                                '- "attendees": array of names mentioned\n'
                                "Be precise. Only extract what is explicitly stated."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Meeting: {event.metadata.get('title', 'Untitled')}\n\nTranscript:\n{event.content[:6000]}",
                        },
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                )

                content = resp.choices[0].message.content or "{}"
                parsed = json.loads(content)

                import hashlib

                meeting_title = event.metadata.get("title", "Meeting")

                # Meeting entity
                mid = hashlib.sha256(
                    f"meeting:{meeting_title}:{event.timestamp.isoformat()}".encode()
                ).hexdigest()[:16]
                all_entities.append(
                    Entity(
                        id=f"ent-{mid}",
                        type=EntityType.MEETING,
                        name=meeting_title,
                        description=parsed.get("summary", ""),
                        attributes={
                            "platform": event.metadata.get("platform", ""),
                            "attendees": parsed.get("attendees", []),
                            "date": event.timestamp.isoformat(),
                        },
                        source=self.source_name,
                        confidence=0.95,
                    )
                )

                for d in parsed.get("decisions", []):
                    did = hashlib.sha256(
                        f"decision:{d.get('decision', '')}".encode()
                    ).hexdigest()[:16]
                    all_entities.append(
                        Entity(
                            id=f"ent-{did}",
                            type=EntityType.DECISION,
                            name=d.get("decision", "")[:100],
                            description=d.get("rationale", ""),
                            attributes={"decided_by": d.get("decided_by", "")},
                            source=self.source_name,
                            confidence=0.9,
                        )
                    )

                for ai in parsed.get("action_items", []):
                    aid = hashlib.sha256(
                        f"action:{ai.get('task', '')}".encode()
                    ).hexdigest()[:16]
                    all_entities.append(
                        Entity(
                            id=f"ent-{aid}",
                            type=EntityType.ACTION_ITEM,
                            name=ai.get("task", "")[:100],
                            description=f"Assigned to {ai.get('assignee', 'TBD')}",
                            attributes={
                                "assignee": ai.get("assignee", ""),
                                "due_date": ai.get("due_date", ""),
                                "priority": ai.get("priority", "medium"),
                            },
                            source=self.source_name,
                            confidence=0.85,
                        )
                    )

                for r in parsed.get("risks", []):
                    rid = hashlib.sha256(
                        f"risk:{r.get('risk', '')}".encode()
                    ).hexdigest()[:16]
                    all_entities.append(
                        Entity(
                            id=f"ent-{rid}",
                            type=EntityType.RISK,
                            name=r.get("risk", "")[:100],
                            description=f"Severity: {r.get('severity', 'medium')}",
                            attributes={
                                "severity": r.get("severity", ""),
                                "owner": r.get("owner", ""),
                            },
                            source=self.source_name,
                            confidence=0.8,
                        )
                    )

            except Exception as exc:
                logger.error("Meeting extraction failed: %s", exc)
                self._sync_status.errors += 1
                self._sync_status.error_messages.append(str(exc))

        return all_entities
