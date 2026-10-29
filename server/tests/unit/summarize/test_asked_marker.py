"""The "asked for" marker on action items is the typed field ``DraftItem.asked``.

A spoken to-do command marks the items it asked for, and ``_mark_explicit`` reads the
marker later. It is the rules' own note: the stored row never has it, so an item can be
logged or serialized as JSON at any point.
"""

import json

from athena.summarize.draft import DraftItem
from athena.summarize.rules import commands, facts


def test_asked_marker_is_never_stored() -> None:
    item = DraftItem(text="Buy eggs", asked=True,
                     command=("Buy eggs", False), said_time="5 pm")
    row = item.storage()
    assert not {"asked", "command", "said_time", "time", "_asked", "_said"} & set(row)


def test_item_marked_asked_serializes_between_passes() -> None:
    items: list[DraftItem] = []
    commands._add_asked(items, "Buy eggs and milk", "Groceries", None, 12.0, "Me")
    assert len(items) == 1
    item = items[0]
    assert item.asked is True
    assert json.loads(json.dumps(item.storage()))["list_name"] == "Groceries"

    commands._mark_explicit(items, None)
    assert item.explicit is True
    assert item.storage()["explicit"] is True


def test_twin_item_gets_the_marker() -> None:
    items = [DraftItem(text="Buy eggs for the cake")]
    commands._add_asked(items, "Buy eggs", None, None, 3.0, "Me")
    assert len(items) == 1
    assert items[0].asked is True
    json.dumps(items[0].storage())


def test_request_check_still_finds_asks() -> None:
    assert facts._request("Priya asked me to send the slides.", [])
    assert facts._request("I told Bo not to call before noon.", [])
    assert not facts._request("Wants Friday to stay free.", [])
