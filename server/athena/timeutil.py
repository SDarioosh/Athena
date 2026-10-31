"""Times: the one ISO-8601 formatter and the two ways a time is read.

Times are UTC epoch seconds inside the server and ISO-8601 in the API.

* :func:`parse_instant` reads what devices and the hub send (REST ``timestamp``,
  ``written_at``, ``since``/``until``, meeting chat times): ISO-8601 with a naive time
  meaning UTC, or epoch seconds (a number above 1e11 is milliseconds).
* :func:`parse_when` reads what a model sends to an MCP tool (``since``/``until``): a
  ``YYYY-MM-DD`` day is local midnight (``end``: the midnight after it), other ISO-8601
  with a naive time meaning local time; no epoch numbers.

The two differ on purpose (a device clock is UTC, a person's day is local); the table in
``tests/unit/api/test_time_parsers.py`` pins both.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo


def iso(ts: float | None) -> str | None:
    """Epoch seconds as ISO-8601 in UTC (``2026-10-18T10:00:00+00:00``); None stays None."""
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


def parse_instant(value: str | float) -> float:
    """ISO-8601 (naive = UTC) or epoch seconds/milliseconds. Raises ValueError for
    anything else, NaN or infinity included. No range check: callers that need one
    (written notes, meeting chat) make it."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        pass
    else:
        if not math.isfinite(num):
            raise ValueError(f"{value!r} is not a finite number")
        return num / 1000.0 if num > 1e11 else num
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def parse_when(value: str, tz: tzinfo, *, end: bool = False) -> float | None:
    """'' -> None; 'YYYY-MM-DD' -> local midnight (``end``: midnight after); ISO -> as given."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
        return (day + timedelta(days=1) if end else day).timestamp()
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"bad date {value!r}; use YYYY-MM-DD") from e
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()


def local_time(ts: float | None, timezone: str, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    """Epoch seconds as local wall-clock text in ``timezone``; None stays None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo(timezone)).strftime(fmt)
