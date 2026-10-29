"""athena.timeutil: the shared time helpers (the parsers' table is test_time_parsers.py)."""

from __future__ import annotations

import math

import pytest

from athena import api, links, mcp_server, timeutil
from athena.meetings.ingest import chat


def test_old_names_are_the_shared_helpers() -> None:
    assert api.iso is timeutil.iso
    assert mcp_server._iso is timeutil.iso
    assert mcp_server.parse_when is timeutil.parse_when


@pytest.mark.parametrize("value, want", [
    ("1792317600", 1792317600.0), ("1792317600000", 1792317600.0),
    (1792317600.5, 1792317600.5), (1792317600000, 1792317600.0),
    ("2026-10-18T10:00:00", 1792317600.0), ("2026-10-18T12:00:00+02:00", 1792317600.0)])
def test_parse_instant(value: str | float, want: float) -> None:
    assert timeutil.parse_instant(value) == want


@pytest.mark.parametrize("value", ["", "nan", "inf", math.nan, math.inf, "garbage", "today"])
def test_parse_instant_rejects(value: str | float) -> None:
    with pytest.raises(ValueError):
        timeutil.parse_instant(value)


@pytest.mark.parametrize("value, want", [
    (1792317600.0, 1792317600.0), (1792317600000.0, 1792317600.0), (None, None)])
def test_chat_times_as_numbers(value: float | None, want: float | None) -> None:
    assert chat.parse_time(value, "at") == want


@pytest.mark.parametrize("value, message", [
    (math.nan, "at must be ISO-8601 or epoch seconds"),
    (-math.inf, "at must be ISO-8601 or epoch seconds"),
    (0.0, "at must be a time between 2000 and 2100")])
def test_chat_rejects_numbers(value: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        chat.parse_time(value, "at")


def test_local_time(settings) -> None:
    assert timeutil.local_time(None, "Europe/Lisbon") is None
    assert timeutil.local_time(1792317600, "Europe/Lisbon") == "2026-10-18 11:00"
    assert timeutil.local_time(1792317600, "UTC", "%H:%M:%S") == "10:00:00"
    assert links.local_time(settings, 1792317600, "%Y") == "2026"
