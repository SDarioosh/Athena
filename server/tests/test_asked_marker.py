"""The "asked for" marker on action items is the string key ``"_asked"``.

A spoken to-do command marks the items it asked for, and ``_mark_explicit`` pops the
marker later. Between those passes the items are plain dicts: the marker must be a
string key, so an item can be logged or serialized as JSON.
"""

import json

from athena.summarize.rules import commands, facts


def test_asked_marker_is_a_string_key() -> None:
    assert commands._ASKED == "_asked"


def test_item_marked_asked_serializes_between_passes() -> None:
    items: list[dict] = []
    commands._add_asked(items, "Buy eggs and milk", "Groceries", None, 12.0, "Me")
    assert len(items) == 1
    item = items[0]
    assert item["_asked"] is True
    assert json.loads(json.dumps(item))["_asked"] is True

    commands._mark_explicit(items, None)
    assert item["explicit"] is True
    assert "_asked" not in item


def test_twin_item_gets_the_marker_as_a_string_key() -> None:
    items: list[dict] = [{"text": "Buy eggs for the cake", "kind": "task"}]
    commands._add_asked(items, "Buy eggs", None, None, 3.0, "Me")
    assert len(items) == 1
    assert items[0]["_asked"] is True
    json.dumps(items)


def test_request_check_still_finds_asks() -> None:
    assert facts._request("Priya asked me to send the slides.", [])
    assert facts._request("I told Bo not to call before noon.", [])
    assert not facts._request("Wants Friday to stay free.", [])
