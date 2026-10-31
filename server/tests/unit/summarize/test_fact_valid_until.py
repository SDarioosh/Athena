"""Facts about one occasion ("has a meeting with the Meshtastic team on Tuesday at 11
a.m.") carry the last day they are worth keeping (``valid_until`` in list_facts), so the
hub's belief ends after it. Fictional data."""

from __future__ import annotations

import json
from datetime import date

import pytest

from athena.dates import fact_valid_until
from athena.mcp_server import build_mcp
from tests.unit.mcp.test_mcp import call
from tests.unit.store.test_confirmed_facts import DAY, T0, say

FRIDAY = date(2026, 10, 23)


@pytest.mark.parametrize("fact,want", [
    ("Shahyah has a meeting with the Meshtastic team on Tuesday at 11 a.m.", "2026-10-27"),
    ("The owner has a dentist appointment tomorrow.", "2026-10-24"),
    ("Bob is in Berlin until December 3.", "2026-12-03"),
    ("The wedding is on 2026-11-07.", "2026-11-07"),
    ("The owner is busy this weekend.", "2026-10-25"),
    ("The owner flies to Lisbon next week.", "2026-11-01"),
    ("The report is due next month.", "2026-11-30"),
    ("The owner is moving in May.", "2027-05-31"),
    ("The owner plans to finish the board by the end of March.", "2027-03-31"),
    ("The owner's last day at work is next Friday.", "2026-10-30"),
])
def test_one_occasion_ends_on_its_day(fact, want):
    assert fact_valid_until(fact, FRIDAY) == date.fromisoformat(want)


@pytest.mark.parametrize("fact", [
    "The owner is vegetarian.",
    "The owner may visit Lisbon.",  # "may" is no month
    "Alice's birthday is June 14.",  # repeats every year
    "The team meets every Tuesday at 10 am.",
    "Bob works on Mondays.",
    "The owner moved here on March 3.",  # already happened
    "The owner went to Lisbon last week.",
    "The owner has lived in Berlin since May.",
    "Alice was born on June 14.",
    "The owner's sabbatical starts on 2028-01-01.",  # more than a year ahead
    "",
])
def test_no_end_date(fact):
    assert fact_valid_until(fact, FRIDAY) is None


def facts_json(pipeline) -> dict[str, dict]:
    out = json.loads(call(build_mcp(pipeline.store, pipeline.settings), "list_facts",
                          format="json"))
    return {f["fact"]: f for f in out["facts"]}


def test_list_facts_json_has_valid_until(pipeline):
    # T0 is Thursday 2025-10-09 (UTC, the test settings' zone)
    say(pipeline.store, "a", T0, "Me", "Me has a meeting with the board on Tuesday at 2 pm")
    say(pipeline.store, "b", T0, "Alice", "Alice is vegetarian")
    rows = facts_json(pipeline)
    assert rows["Me has a meeting with the board on Tuesday at 2 pm"]["valid_until"] \
        == "2025-10-14"
    assert rows["Alice is vegetarian"]["valid_until"] is None


def test_counted_from_the_day_the_fact_was_first_said(pipeline):
    store = pipeline.store
    _, first = say(store, "a", T0, "Me", "Me has a meeting with the board on Tuesday")
    _, again = say(store, "b", T0 + 2 * DAY, "Me", "Me has a meeting with the board on Tuesday")
    store.link_fact(again, "same", first)
    (row,) = facts_json(pipeline).values()
    assert row["valid_until"] == "2025-10-14"


def test_uses_the_module_time_zone(pipeline):
    # 23:30 UTC on Thursday is already Friday in Tokyo: "tomorrow" is Saturday there
    late = T0 + 14.6 * 3600
    say(pipeline.store, "a", late, "Me", "Me has a dentist appointment tomorrow")
    utc = facts_json(pipeline)["Me has a dentist appointment tomorrow"]["valid_until"]
    pipeline.settings = pipeline.settings.model_copy(update={"timezone": "Asia/Tokyo"})
    tokyo = facts_json(pipeline)["Me has a dentist appointment tomorrow"]["valid_until"]
    assert (utc, tokyo) == ("2025-10-10", "2025-10-11")


def test_text_format_says_until(pipeline):
    say(pipeline.store, "a", T0, "Me", "Me has a meeting with the board on Tuesday at 2 pm")
    say(pipeline.store, "b", T0, "Alice", "Alice is vegetarian")
    text = call(build_mcp(pipeline.store, pipeline.settings), "list_facts")
    line = next(x for x in text.splitlines() if "board" in x)
    assert "(learned 2025-10-09, until 2025-10-14)" in line
    assert "until" not in next(x for x in text.splitlines() if "vegetarian" in x)
