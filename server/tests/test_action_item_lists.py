"""The to-do list the user names for an action item ("add milk to my shopping list"):
picked up from the voice note, stored (v7 migration), listed and changed over MCP and
the HTTP API. Fictional data only."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_action_items import T0, note_items, old_v5_db, summarizer
from test_mcp import call
from test_voice_notes import conv_of, record

from convo_server.llm import ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM
from convo_server.mcp_server import build_mcp
from convo_server.store import Store, clean_list_name

LIST_ITEMS = [
    {"text": "Milk", "kind": "task", "list_name": "my shopping list", "at_seconds": 1},
    # the model left the phrase in the text and gave no list: taken from the text
    {"text": "Add eggs to my shopping list", "kind": "task", "at_seconds": 2},
    {"text": "Put call the bank on my Work to-do list", "kind": "task", "at_seconds": 3},
    {"text": "Call the plumber", "kind": "task", "at_seconds": 4},
]


@pytest.mark.parametrize(("said", "name"), [
    ("shopping", "shopping"), ("my shopping list", "shopping"),
    ("The Work To-Do list", "Work"), ("  groceries  ", "groceries"),
    ("to-do list", None), ("my todo list", None), ("todos", None), ("To-dos", None),
    ("list", None), ("", None), (None, None), ("x" * 41, None),
])
def test_clean_list_name(said, name):
    assert clean_list_name(said) == name


def test_note_items_get_their_list():
    got = [(a["text"], a["list_name"]) for a in note_items(LIST_ITEMS, said="A few things to do.")]
    assert got == [("Milk", "shopping"), ("Eggs", "shopping"), ("Call the bank", "Work"),
                   ("Call the plumber", None)]


@pytest.mark.parametrize(("text", "said", "want"), [
    # a list the text only mentions in passing is not a list to file on
    ("Check in on the waiting list", None, ("Check in on the waiting list", None)),
    ("Add Bob to the guest list", "guest", ("Bob", "guest")),
    # no "my" / "the": still the named list
    ("Add oil to shopping list", None, ("Oil", "shopping")),
    ("Add soda to Shopping List.", None, ("Soda", "Shopping")),
    ("Put batteries on hardware store list", None, ("Batteries", "hardware store")),
    # without "the", a list name holds no "the" / "a": no list here
    ("Add a reminder to check the list", None, ("Add a reminder to check the list", None)),
    ("Add notes to a list", None, ("Add notes to a list", None)),
    # the model's list wins; a phrase naming another list stays in the text
    ("Add tape to the office list", "home", ("Add tape to the office list", "home")),
    # the general to-do list is no named list: a to-do of its own, as before
    ("Add a todo for calling the bank", None, ("Add a todo for calling the bank", None)),
    ("Put call the bank on my to-do list", None, ("Put call the bank on my to-do list", None)),
    ("Add call the bank to to-do list", None, ("Add call the bank to to-do list", None)),
    ("Call the bank", "todos", ("Call the bank", None)),
])
def test_list_phrase_in_text(text, said, want):
    (item,) = note_items([{"text": text, "list_name": said}], said="A few things to do.")
    assert (item["text"], item["list_name"]) == want


def test_both_action_prompts_ask_for_the_list():
    for prompt in (ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM):
        text = prompt.format(date="Thursday 2025-10-09", owner="Me")
        assert "list_name" in text and "shopping list" in text


def test_migration_to_v7(tmp_path):
    path = tmp_path / "old.db"
    old_v5_db(path)
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 13
    assert store.list_action_items()[0]["list_name"] is None
    store.close()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(action_items)")}
    assert "list_name" in cols


def items_by_text(pipeline):
    return {a["text"]: a for a in conv_of(pipeline, "n1")["action_items"]}


def test_list_round_trips_and_user_change_survives_resummary(pipeline):
    pipeline.summarizer = summarizer(LIST_ITEMS)
    record(pipeline, "n1", T0, "note")
    store = pipeline.store
    before = items_by_text(pipeline)
    assert before["Milk"]["list_name"] == "shopping"
    got = store.update_action_item(before["Call the plumber"]["id"], list_name="Home list")
    assert got["list_name"] == "Home" and got["edited"] == 1
    assert store.update_action_item(before["Eggs"]["id"], list_name=None)["list_name"] is None
    with pytest.raises(ValueError, match="list_name"):
        store.update_action_item(before["Milk"]["id"], list_name="my list")

    conv_id = conv_of(pipeline, "n1")["id"]
    segs = store.conversation_segments(conv_id)
    store.save_summary(conv_id, pipeline.summarizer.summarize(segs, kind="note"))
    after = items_by_text(pipeline)
    # "Call the bank", said a second earlier and alike, must not take the plumber's row
    assert {t: a["id"] for t, a in after.items()} == {t: a["id"] for t, a in before.items()}
    assert after["Call the plumber"]["list_name"] == "Home"  # the user's change
    assert after["Eggs"]["list_name"] is None
    assert after["Milk"]["list_name"] == "shopping"


@pytest.fixture
def list_mcp(pipeline):
    pipeline.summarizer = summarizer(LIST_ITEMS)
    record(pipeline, "n1", T0, "note")
    return build_mcp(pipeline.store, pipeline.settings)


def test_mcp_lists_and_changes_the_list(pipeline, list_mcp):
    rows = {r["text"]: r for r in json.loads(
        call(list_mcp, "list_action_items", format="json"))}
    assert rows["Milk"]["list_name"] == "shopping"
    assert rows["Call the plumber"]["list_name"] is None
    text = call(list_mcp, "list_action_items")
    assert "Milk (owner" not in text  # no owner in these items
    assert any("Milk [list shopping] — from" in ln for ln in text.splitlines())

    plumber = rows["Call the plumber"]["id"]
    assert call(list_mcp, "update_action_item", item_id=plumber, list_name="home") == (
        f"Action item #{plumber} is now open, task, no due date, on the home list.")
    assert call(list_mcp, "update_action_item", item_id=plumber, list_name="none") == (
        f"Action item #{plumber} is now open, task, no due date.")
    assert "list_name must be" in call(list_mcp, "update_action_item", item_id=plumber,
                                       list_name="list")


def test_http_patch_list(client, pipeline):
    pipeline.summarizer = summarizer(LIST_ITEMS)
    record(pipeline, "n1", T0, "note")
    rows = client.get("/v1/action-items").json()["action_items"]
    milk = next(r for r in rows if r["text"] == "Milk")
    assert milk["list_name"] == "shopping"
    url = f"/v1/action-items/{milk['id']}"
    assert client.patch(url, json={"list_name": "groceries"}).status_code == 200
    assert client.patch(url, json={"list_name": "list"}).status_code == 400
    assert client.patch(url, json={"list_name": None}).status_code == 200
    got = next(r for r in client.get("/v1/action-items").json()["action_items"]
               if r["id"] == milk["id"])
    assert got["list_name"] is None
