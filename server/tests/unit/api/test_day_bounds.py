"""``Pipeline.day_bounds`` (a digest's local day) against ``timeutil.parse_when``'s date
branch. They agree on every ``YYYY-MM-DD`` day, DST changes included, but ``day_bounds``
is also the digest route's date check (``POST /v1/digests/{date}`` answers 400 when it
raises), and ``parse_when`` takes more: '' (None), spaces around the date and any other
ISO-8601 time. So ``day_bounds`` keeps its own strict parse instead of going through
``parse_when``."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from athena.timeutil import parse_when

ZONES = ["UTC", "Europe/Berlin", "America/New_York", "Australia/Lord_Howe", "Asia/Kolkata"]
DAYS = ["2025-10-09", "2025-03-30", "2025-10-26", "2025-03-09", "2025-11-02",
        "2024-02-29", "2025-12-31", "2000-01-01", "2099-12-31", "2025-4-5"]


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("day", DAYS)
def test_the_same_bounds_for_every_day(pipeline, zone, day):
    pipeline.settings.timezone = zone
    tz = ZoneInfo(zone)
    assert pipeline.day_bounds(day) == (parse_when(day, tz), parse_when(day, tz, end=True))


@pytest.mark.parametrize(("value", "parse_when_gives"), [
    ("", None),
    (" 2025-10-09 ", "day"),
    ("2025-10-09T10:00", "time"),
    ("2025-10-09T10:00:00+02:00", "time"),
    ("2025-10-09 10:00", "time"),
    ("20251009", "day"),  # ISO-8601's basic format
])
def test_day_bounds_refuses_what_parse_when_takes(pipeline, value, parse_when_gives):
    with pytest.raises(ValueError):
        pipeline.day_bounds(value)
    got = parse_when(value, ZoneInfo("UTC"))
    assert (got is None) == (parse_when_gives is None)


@pytest.mark.parametrize("value", ["2025-13-01", "2025-02-30", "yesterday", "2025/10/09"])
def test_both_refuse_what_is_no_date(pipeline, value):
    with pytest.raises(ValueError):
        pipeline.day_bounds(value)
    with pytest.raises(ValueError):
        parse_when(value, ZoneInfo("UTC"))
