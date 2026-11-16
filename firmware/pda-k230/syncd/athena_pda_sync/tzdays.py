"""The LOCAL `tz` message (docs/k230-pda-plan.md, Contract 1): the UTC offset of each of
the next 14 local days, so the UI's new-event form gives an event on a day after a DST
change that day's offset instead of today's (TIME carries one offset only, as on the T5).

`{"t": "tz", "day": <local day number of the first>, "offsets": [<minutes>, ...]}`: the
day number counts days since 1970-01-01 of the local date (the UI's `pda_local_day`), each
offset is the one in effect at local noon of that day. Sent on connect and whenever it
changes (the day turned, or `tz` in config.json changed).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

UTC = timezone.utc  # noqa: UP017 (Python 3.10 has no datetime.UTC)
DAYS = 14
EPOCH = date(1970, 1, 1)


def message(tz: tzinfo, now: float, days: int = DAYS) -> dict[str, Any]:
    today = datetime.fromtimestamp(now, UTC).astimezone(tz).date()
    offsets = []
    for i in range(days):
        noon = datetime.combine(today + timedelta(days=i), time(12), tz)
        offsets.append(int((noon.utcoffset() or timedelta(0)).total_seconds() // 60))
    return {"t": "tz", "day": (today - EPOCH).days, "offsets": offsets}
