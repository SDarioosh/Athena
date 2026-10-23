"""Action items have a short title and say whether the user asked for them (explicit):
the hub files explicit items itself and offers the rest as suggestions."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import fake_llm_transport
from test_mcp import call
from test_voice_notes import record

from convo_server.llm import LLMClient, Summarizer
from convo_server.mcp_server import build_mcp
from convo_server.store import Segment, Store, short_title
from convo_server.transcribe import TranscriptPiece

T0 = 1_760_000_000.0  # Thursday 2025-10-09


def summarizer(items: list[dict]) -> Summarizer:
    summary = {"title": "Errands", "summary": "s", "key_points": [], "people": [],
               "topics": [], "facts": [], "action_items": items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    return Summarizer(llm, "Me", timezone="Europe/Berlin")


def items_of(items: list[dict], said: str, kind: str = "note") -> list[dict]:
    return summarizer(items).summarize([Segment(T0, T0 + 5, said, "Me", True)],
                                       kind=kind)["action_items"]


# -------------------------------------------------------------------------- titles
@pytest.mark.parametrize("title,text,want", [
    ("Call the plumber about the tap", "x", "Call the plumber about the tap"),
    ("  Call  the plumber. ", "x", "Call the plumber"),
    (None, "Call the plumber about the leaking tap in the upstairs bathroom before Friday",
     "Call the plumber about the leaking tap in the upstairs"),
    ("", "Send the slides.", "Send the slides"),
    (None, "x" * 70, "x" * 60),
])
def test_short_title(title, text, want):
    got = short_title(title, text)
    assert got == want and len(got) <= 60


def test_model_title_and_fallback():
    long = ("Buy the replacement brake pads for the bike at the shop on Lindenstrasse "
            "this week")
    items = items_of([{"text": "Call the plumber about the leaking tap",
                       "title": "Call the plumber about the tap"},
                      {"text": long}], "Call the plumber about the leaking tap. " + long)
    assert [i["title"] for i in items] == [
        "Call the plumber about the tap",
        "Buy the replacement brake pads for the bike at the shop on"]


def test_note_title_is_the_notes_title():
    (item,) = items_of([{"text": "Battery dimensions are 13 mm x 14 mm x 3 mm",
                         "kind": "note", "title": "Battery dimensions"}],
                       "Create a note battery dimensions are 13mm x 14mm x 3mm")
    assert (item["title"], item["note_title"]) == ("Battery dimensions", "Battery dimensions")


# ------------------------------------------------------------------------ explicit
@pytest.mark.parametrize("said,item,explicit", [
    ("Remind me Friday to send the slides.",
     {"text": "Send the slides", "kind": "reminder", "due": "Friday"}, True),
    ("Add milk to my shopping list.", {"text": "Add milk to my shopping list"}, True),
    ("Call the plumber on Friday.", {"text": "Call the plumber", "due": "Friday"}, True),
    ("Meeting with Jason at noon.", {"text": "Meet Jason", "kind": "event"}, True),
    ("Add a task, call the bank.", {"text": "Call the bank"}, True),
    ("To-do: renew the passport.", {"text": "Renew the passport"}, True),
    ("Task: water the plants.", {"text": "Water the plants"}, True),
    ("Note that I promised Greta the slides.", {"text": "Send Greta the slides"}, True),
    ("Call the plumber about the tap.", {"text": "Call the plumber about the tap"}, False),
    ("I should email Bob about the invoice.", {"text": "Email Bob about the invoice"},
     False),
])
def test_explicit_in_voice_notes(said, item, explicit):
    (got,) = items_of([item], said)
    assert got["explicit"] is explicit


@pytest.mark.parametrize("said", [
    "Create a to-do list and get Rock Talk ready for the meeting with the Meshtastic team.",
    "Create a to do list. Get Rock Talk ready for the meeting with the Meshtastic team.",
    "Okay, make a to-do list for tomorrow. Get Rock Talk ready for the Meshtastic meeting.",
    "To-do list: get Rock Talk ready for the meeting with the Meshtastic team.",
])
def test_items_after_a_to_do_list_sentence_are_explicit(said):
    (got,) = items_of([{"text": "Get Rock Talk ready for meeting with Meshtastic team"}], said)
    assert got["explicit"] is True


def test_list_sentence_covers_later_items_only():
    items = items_of([{"text": "Call the plumber about the tap"},
                      {"text": "Book the train to Leipzig"},
                      {"text": "Email Bob about the invoice"}],
                     "Call the plumber about the tap. Now make a to-do list. "
                     "Book the train to Leipzig. I need to email Bob about the invoice.")
    assert [(i["text"], i["explicit"]) for i in items] == [
        ("Call the plumber about the tap", False), ("Book the train to Leipzig", True),
        ("Email Bob about the invoice", True)]


def test_lots_to_do_is_no_list_sentence():
    (got,) = items_of([{"text": "Book the train to Leipzig"}],
                      "I have a lot to do this week. Book the train to Leipzig.")
    assert got["explicit"] is False


def test_conversations_count_dates_lists_and_kinds_only():
    items = items_of([{"text": "Send Sam the slides", "due": "Friday"},
                      {"text": "Water Lena's plants"}],
                     "Remind me, can you water the plants? I'll send the slides Friday.",
                     kind="casual")
    assert [(i["text"], i["explicit"]) for i in items] == [
        ("Send Sam the slides", True), ("Water Lena's plants", False)]


# ------------------------------------------------------------------- storage and MCP
@pytest.fixture
def stored(pipeline):
    def transcribe(audio, prompt=""):
        return [TranscriptPiece(0.0, audio.size / 16000,
                                "Remind me Friday to send the slides. "
                                "Call the plumber about the tap.")]
    pipeline.transcriber.transcribe = transcribe
    pipeline.settings.timezone = "Europe/Berlin"
    pipeline.summarizer = summarizer([
        {"text": "Send the slides", "kind": "reminder", "due": "Friday",
         "title": "Send the slides to Greta", "at_seconds": 0},
        {"text": "Call the plumber about the leaking tap", "at_seconds": 0}])
    record(pipeline, "n1", T0, "note")
    return pipeline


def rows(pipeline) -> dict[str, dict]:
    mcp = build_mcp(pipeline.store, pipeline.settings)
    return {r["text"]: r for r in json.loads(call(mcp, "list_action_items", format="json"))}


def test_json_rows_have_title_and_explicit(stored):
    got = rows(stored)
    slides, plumber = got["Send the slides"], got["Call the plumber about the leaking tap"]
    assert (slides["title"], slides["explicit"]) == ("Send the slides to Greta", True)
    assert (plumber["title"], plumber["explicit"]) == (
        "Call the plumber about the leaking tap", False)
    # the text listing is unchanged
    text = call(build_mcp(stored.store, stored.settings), "list_action_items")
    assert "Send the slides to Greta" not in text and "explicit" not in text


def test_update_title_and_explicit(stored, client):
    mcp = build_mcp(stored.store, stored.settings)
    plumber = rows(stored)["Call the plumber about the leaking tap"]
    call(mcp, "update_action_item", item_id=plumber["id"], title="Plumber: tap")
    assert rows(stored)[plumber["text"]]["title"] == "Plumber: tap"
    assert rows(stored)[plumber["text"]]["explicit"] is False  # a title asks for nothing
    call(mcp, "update_action_item", item_id=plumber["id"], title="none")
    assert rows(stored)[plumber["text"]]["title"] == "Call the plumber about the leaking tap"
    # a date the user gives makes it theirs
    call(mcp, "update_action_item", item_id=plumber["id"], due_date="2025-10-13")
    assert rows(stored)[plumber["text"]]["explicit"] is True
    r = client.patch(f"/v1/action-items/{plumber['id']}", json={"title": "Tap"})
    assert r.status_code == 200
    item = next(i for i in client.get("/v1/action-items").json()["action_items"]
                if i["id"] == plumber["id"])
    assert (item["title"], item["explicit"]) == ("Tap", True)


def test_resummary_keeps_an_edited_title(stored):
    plumber = rows(stored)["Call the plumber about the leaking tap"]
    stored.store.update_action_item(plumber["id"], title="Plumber")
    (conv_id,) = stored.store.session_conversations("n1")
    assert stored.summarize_conversation(conv_id)
    again = rows(stored)[plumber["text"]]
    assert (again["id"], again["title"]) == (plumber["id"], "Plumber")


def test_migration_gives_old_items_a_title_and_explicit(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35):  # DROP COLUMN makes the old table
        pytest.skip("SQLite 3.35+ needed")
    path = tmp_path / "convo.db"
    store = Store(path)
    c = store._conn
    c.execute("INSERT INTO conversations (id, device, started_at, ended_at, status,"
              " updated_at) VALUES ('c1', 'omi', 0, 10, 'summarized', 0)")
    c.execute("INSERT INTO action_items (conversation_id, text, kind, list_name)"
              " VALUES ('c1', 'Buy the replacement brake pads for the bike at the shop on"
              " Lindenstrasse', 'task', NULL), ('c1', 'Milk', 'task', 'shopping')")
    c.execute("ALTER TABLE action_items DROP COLUMN title")
    c.execute("ALTER TABLE action_items DROP COLUMN explicit")
    c.execute("PRAGMA user_version = 10")
    c.commit()
    store.close()
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 13
    got = {r["text"]: r for r in map(dict, store._conn.execute(
        "SELECT * FROM action_items").fetchall())}
    brakes = got["Buy the replacement brake pads for the bike at the shop on Lindenstrasse"]
    assert (brakes["title"], brakes["explicit"]) == (
        "Buy the replacement brake pads for the bike at the shop on", 0)
    assert (got["Milk"]["title"], got["Milk"]["explicit"]) == ("Milk", 1)
