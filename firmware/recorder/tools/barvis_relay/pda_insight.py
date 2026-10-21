"""The hub's "Today insight" for the T5's Today screen (docs/t5-pda-plan.md §4.1).

- Read: `GET /api/codex/insight/today` (scope insight:read) -> `{"date", "generated",
  "headline", "focus": [{"ref", "why", "title"}], "coming_up": [...], "also": [...]}`, or
  404 `{"error": "no insight yet"}`.
- `today()` returns that answer, or None for "no insight" and logs why: no insight yet
  (404), a hub without the route (404 "Not Found", 405), a token without insight:read (403),
  any other final 4xx, or a 200 that isn't a JSON object. Network errors, 5xx, 408/429 and
  401 are an `UploadError`, which fails the feed like a failing calendar.

`pda_feed` checks the answer's shape and keeps only refs that resolve in the feed. Same
token, URL and request rules as the calendar (`OdysseusCalendar`, `Uploader`).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .uploader import SKIP, Uploader, UploadError

log = logging.getLogger("barvis_relay.pda")

INSIGHT = "/api/codex/insight/today"

# Why the feed has no `insight` (the same words as the Android relay's)
NOT_YET = "no insight yet"
NO_SCOPE = "token lacks insight:read"
OLD_HUB = "this Odysseus has no insight route (update the hub)"
MALFORMED = "malformed insight answer"


class Insight(Protocol):
    def today(self) -> dict[str, Any] | None:
        """The hub's answer, or None if there is no insight to show (logged)."""


class OdysseusInsight(Uploader):
    def __repr__(self) -> str:
        return f"OdysseusInsight({self.server!r})"

    def today(self) -> dict[str, Any] | None:
        try:
            return self._request("GET", INSIGHT)
        except UploadError as e:
            why = reason(e)
            if why is None:
                raise
            log.log(logging.INFO if why == NOT_YET else logging.WARNING,
                    "Odysseus insight: %s; the feed has no insight", why)
            return None


def reason(e: UploadError) -> str | None:
    """Why `e` means "no insight" rather than a failed feed, or None if it is a failure."""
    if e.status == 200:  # 200 but not a JSON object
        return MALFORMED
    if e.status == 403:
        return NO_SCOPE
    if e.status == 405 or (e.status == 404 and str(e).endswith(" Not Found")):
        return OLD_HUB  # FastAPI's answer for a missing route
    if e.status == 404:
        return NOT_YET
    if e.kind == SKIP and e.status is not None:  # another final 4xx
        return f"Odysseus insight: HTTP {e.status}"
    return None  # network error, 5xx, 408/429, 401: a failure, like the calendar's
