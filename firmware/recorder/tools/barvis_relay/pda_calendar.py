"""The Odysseus calendar over its token REST routes (docs/t5-pda-plan.md §6).

- List: `GET /api/codex/calendar/events?start=<ISO>&end=<ISO>` -> `{"events": [...]}`.
- Create: `POST /api/codex/calendar/events` with `summary`, `dtstart`, optional `dtend`,
  `all_day`, `calendar_href` -> `{"ok": true, "uid": ...}`. Not idempotent: the relay's
  cal_add dedupe (pda_actions.CalendarMemory) keeps a re-sent action from making two events.
- Delete: `DELETE /api/codex/calendar/events/{uid}` -> `{"ok": true}`, 404 if gone. It
  deletes the whole series, so an occurrence id (`<uid>::<occurrence>`) is refused.

Auth is `Authorization: Bearer ody_...` only (a token scoped to calendar:read and
calendar:write), with the same rules as `Uploader`: never logged, never in a URL, no
redirects. Errors are `UploadError` (RETRY, AUTH, or SKIP for a final 4xx).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .pda_actions import CalAdd
from .uploader import RETRY, SKIP, Uploader, UploadError

log = logging.getLogger("barvis_relay.pda")

EVENTS = "/api/codex/calendar/events"


class OdysseusCalendar(Uploader):
    """`pda_actions.Calendar` for Odysseus. `calendar_href` is the calendar new events go
    to; without it Odysseus picks the owner's first calendar, which may not be CalDAV."""

    def __init__(self, server: str, token: str, *, calendar_href: str | None = None,
                 timeout: float = 25.0) -> None:
        super().__init__(server, token, timeout=timeout)
        self.calendar_href = calendar_href or None

    def __repr__(self) -> str:
        return f"OdysseusCalendar({self.server!r})"

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        query = urlencode({"start": start.isoformat(), "end": end.isoformat()})
        answer = self._request("GET", f"{EVENTS}?{query}")
        if answer.get("truncated"):
            log.warning("the calendar list was truncated by Odysseus")
        out = []
        for ev in answer.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("uid"):
                continue
            out.append({"id": str(ev["uid"]), "title": ev.get("summary") or "",
                        "start": ev.get("dtstart"), "end": ev.get("dtend"),
                        "where": ev.get("location") or None,
                        "all_day": bool(ev.get("all_day"))})
        return out

    def create(self, event: CalAdd) -> str:
        body: dict[str, Any] = {"summary": event.title, "dtstart": event.start,
                                "all_day": event.all_day}
        if event.end:
            body["dtend"] = event.end
        if self.calendar_href:
            body["calendar_href"] = self.calendar_href
        answer = self._request("POST", EVENTS, body=json.dumps(body).encode(),
                               content_type="application/json")
        uid = answer.get("uid")
        if answer.get("ok") is not True or not uid:
            raise UploadError(RETRY, "calendar create answered without ok/uid", 200)
        return str(uid)

    def delete(self, event_id: str) -> None:
        from urllib.parse import quote

        if "::" in event_id:  # Odysseus would delete the whole series
            raise UploadError(SKIP, "one occurrence of a recurring event can't be deleted",
                              400)
        self._request("DELETE", f"{EVENTS}/{quote(event_id, safe='')}")
