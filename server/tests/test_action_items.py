"""Action items for the calendar: kind (task/event/reminder), time of day, length, place;
stored with a v6 migration, listed as text or JSON over MCP, changed with
update_action_item. Fictional data only."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import fake_llm_transport
from test_mcp import call
from test_voice_notes import conv_of, record

from athena import store as store_mod
from athena.llm import LLMClient, Summarizer
from athena.mcp_server import build_mcp
from athena.store import Segment, Store

T0 = 1_760_000_000.0  # Thursday 2025-10-09 08:53:20 UTC = 10:53 in Berlin

NOTE_ITEMS = [
    {"text": "Meet Jason", "kind": "event", "owner": "Me", "due": "Wednesday",
     "time": "12 pm", "at_seconds": 1},
    {"text": "Get the proposal done", "kind": "task", "owner": "Me", "due": "on Wednesday",
     "at_seconds": 2},
    {"text": "Call Mom", "kind": "reminder", "owner": "Me", "time": "5", "at_seconds": 3},
    {"text": "Go to the dentist", "kind": "event", "due": "Friday", "time": "3pm",
     "duration_minutes": 60, "location": "Main Street", "at_seconds": 4},
]


def summary(items):
    return {"title": "Plans for the week", "summary": "s", "key_points": [], "people": [],
            "topics": [], "facts": [], "action_items": items}


def summarizer(items):
    llm = LLMClient("http://llm.test/v1", "m",
                    transport=fake_llm_transport(summary=summary(items)))
    return Summarizer(llm, "Me", timezone="Europe/Berlin")


def note_items(items, said="Meeting with Jason on Wednesday at 12 pm"):
    # a line with a day, a time and a meeting gets an event even without the model's
    # item (test_note_events.py): tests of other items say something else
    out = summarizer(items).summarize([Segment(T0, T0 + 5, said, "Me", True)], kind="note")
    return out["action_items"]


# --------------------------------------------------------------- summarizing
def test_voice_note_items_get_kind_date_and_time():
    items = note_items(NOTE_ITEMS)
    got = [(a["text"], a["kind"], a["due_date"], a["due_time"], a["time_unclear"],
            a["duration_minutes"], a["location"]) for a in items]
    assert got == [
        ("Meeting with Jason", "event", "2025-10-15", "12:00", False, None, None),  # as said
        ("Get the proposal done", "task", "2025-10-15", None, False, None, None),
        ("Call Mom", "reminder", "2025-10-09", "17:00", False, None, None),  # today
        ("Go to the dentist", "event", "2025-10-10", "15:00", False, 60, "Main Street"),
    ]
    assert all("time" not in a for a in items)  # the phrase is resolved, not stored


def test_time_details():
    items = note_items([
        # a bare 9 could be the evening; recorded at 10:53, so 9:00 is tomorrow
        {"text": "Remind me to water the plants", "kind": "reminder", "time": "9"},
        {"text": "Call the bank", "kind": "task", "time": "sometime later"},
        # no time from the model: taken from the due phrase
        {"text": "Lunch with Ana", "kind": "event", "due": "Friday at half past one"},
        {"text": "Team standup", "kind": "event", "due": "every Monday", "time": "9:30 am",
         "duration_minutes": 15},
        {"text": "Buy milk", "duration_minutes": -5},  # kind defaults to task
    ], said="A few things to do.")
    plants, bank, lunch, standup, milk = items
    assert (plants["due_date"], plants["due_time"], plants["time_unclear"]) == (
        "2025-10-10", "09:00", True)
    assert (bank["due_time"], bank["time_unclear"], bank["due_date"]) == (None, True, None)
    assert (lunch["due_date"], lunch["due_time"]) == ("2025-10-10", "13:30")
    # repeating: not one calendar event but a task to set it up
    assert standup["kind"] == "task" and standup["duration_minutes"] is None
    assert standup["text"] == "Set up a repeating event: team standup"
    assert (milk["kind"], milk["duration_minutes"], milk["due_time"]) == ("task", None, None)


def test_both_action_prompts_explain_the_kinds():
    from athena.llm import ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM

    for prompt in (ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM):
        text = prompt.format(date="Thursday 2025-10-09", owner="Me")
        for word in ('"event"', '"reminder"', '"task"', "duration_minutes", "location",
                     "Set up a repeating event"):
            assert word in text


# ------------------------------------------------------------------ storage
def old_v5_db(path):
    """A database as version 5 left it: action items without the calendar columns."""
    old = sqlite3.connect(path)
    old.executescript(store_mod.SCHEMA)
    old.executescript(
        "ALTER TABLE conversations ADD COLUMN kind TEXT;"
        "ALTER TABLE conversations ADD COLUMN discard_reason TEXT;"
        "ALTER TABLE action_items ADD COLUMN due_date TEXT;"
        "ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'current';"
        "ALTER TABLE facts ADD COLUMN superseded_by INTEGER;"
        "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'conversation';"
        f"INSERT INTO conversations (id, device, started_at, ended_at, status, title,"
        f" updated_at) VALUES ('c1', 'omi', {T0}, {T0 + 60}, 'summarized', 'Old', {T0});"
        "INSERT INTO action_items (conversation_id, text, due, due_date)"
        " VALUES ('c1', 'Send the slides', 'Friday', '2025-10-10');"
        "PRAGMA user_version = 5;")
    old.close()


def test_migration_v5_to_v6_makes_old_items_tasks(tmp_path):
    path = tmp_path / "old.db"
    old_v5_db(path)
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 17
    (item,) = store.list_action_items()
    assert item["kind"] == "task" and item["due_date"] == "2025-10-10"
    assert item["due_time"] is None and item["time_unclear"] == 0
    assert item["duration_minutes"] is None and item["location"] is None
    assert item["created_at"] == T0  # backfilled with the conversation's time
    store.close()
    assert Store(path).list_action_items()[0]["kind"] == "task"  # reopening is a no-op


def test_new_fields_round_trip(pipeline):
    pipeline.summarizer = summarizer(NOTE_ITEMS)
    record(pipeline, "n1", T0, "note")
    items = conv_of(pipeline, "n1")["action_items"]
    dentist = items[3]
    assert (dentist["kind"], dentist["due_date"], dentist["due_time"],
            dentist["duration_minutes"], dentist["location"], dentist["time_unclear"]) == (
        "event", "2025-10-10", "15:00", 60, "Main Street", 0)
    assert all(a["created_at"] for a in items)
    listed = pipeline.store.list_action_items()
    assert {a["kind"] for a in listed} == {"event", "task", "reminder"}


def test_update_action_item_in_store(pipeline):
    pipeline.summarizer = summarizer(NOTE_ITEMS)
    record(pipeline, "n1", T0, "note")
    store = pipeline.store
    item_id = conv_of(pipeline, "n1")["action_items"][0]["id"]
    store._conn.execute("UPDATE action_items SET time_unclear = 1 WHERE id = ?", (item_id,))
    got = store.update_action_item(item_id, kind="task", due_date="2025-10-20",
                                   due_time="9:05")
    assert (got["kind"], got["due_date"], got["due_time"], got["time_unclear"]) == (
        "task", "2025-10-20", "09:05", 0)
    got = store.update_action_item(item_id, due_date=None, due_time=None)
    assert got["due_date"] is None and got["due_time"] is None and got["kind"] == "task"
    for bad in ({"kind": "meeting"}, {"due_date": "Friday"}, {"due_date": "2025-02-30"},
                {"due_time": "25:00"}, {"status": "gone"}, {}):
        with pytest.raises(ValueError):
            store.update_action_item(item_id, **bad)
    assert store.update_action_item(99_999, status="done") is None


def test_resummary_keeps_ids_status_and_user_edits(pipeline):
    """The hub files items by id: a re-summary (speaker renamed, conversations merged)
    must not hand out new ids, nor undo what the user changed."""
    pipeline.summarizer = summarizer(NOTE_ITEMS)
    record(pipeline, "n1", T0, "note")
    store = pipeline.store
    conv = conv_of(pipeline, "n1")
    before = {a["text"]: a for a in conv["action_items"]}
    store.update_action_item(before["Meet Jason"]["id"], kind="task", due_time="13:00")
    store.set_action_item_status(before["Call Mom"]["id"], "done")

    segs = store.conversation_segments(conv["id"])
    store.save_summary(conv["id"], pipeline.summarizer.summarize(segs, kind="note"))
    after = {a["text"]: a for a in store.get_conversation(conv["id"])["action_items"]}
    assert {t: a["id"] for t, a in after.items()} == {t: a["id"] for t, a in before.items()}
    assert {t: a["created_at"] for t, a in after.items()} == {
        t: a["created_at"] for t, a in before.items()}
    assert (after["Meet Jason"]["kind"], after["Meet Jason"]["due_time"]) == ("task", "13:00")
    assert after["Call Mom"]["status"] == "done"
    assert after["Go to the dentist"]["kind"] == "event"


# ---------------------------------------------------------------------- MCP
@pytest.fixture
def note_mcp(pipeline):
    pipeline.settings.timezone = "Europe/Berlin"
    pipeline.summarizer = summarizer(NOTE_ITEMS)
    record(pipeline, "n1", T0, "note")
    return build_mcp(pipeline.store, pipeline.settings)


def test_list_action_items_text(pipeline, note_mcp):
    out = call(note_mcp, "list_action_items")
    lines = {ln.split("] ", 1)[1].split(" — ")[0]: ln for ln in out.splitlines()
             if ln.startswith("- #")}
    assert set(lines) == {
        "event: Meet Jason (owner Me) due 2025-10-15 12:00 (Wednesday)",
        "Get the proposal done (owner Me) due 2025-10-15 (on Wednesday)",
        "reminder: Call Mom (owner Me) due 2025-10-09 17:00",
        "event: Go to the dentist due 2025-10-10 15:00 (Friday)",
    }
    meet = lines["event: Meet Jason (owner Me) due 2025-10-15 12:00 (Wednesday)"]
    assert '— from "Plans for the week" (voice note) 2025-10-09 10:53 http' in meet
    assert "/view/" in meet and meet.endswith("#t=1")
    assert out.rstrip().endswith("put the link next to each fact you use.")

    item_id = pipeline.store.list_action_items()[0]["id"]
    pipeline.store._conn.execute("UPDATE action_items SET time_unclear = 1 WHERE id = ?",
                                 (item_id,))
    assert "[time unclear] — from" in call(note_mcp, "list_action_items")


def test_list_action_items_json_mine_flag(pipeline, note_mcp):
    pipeline.store._conn.execute(
        "UPDATE action_items SET owner = 'Marisol' WHERE text = 'Meet Jason'")
    items = json.loads(call(note_mcp, "list_action_items", status="all", format="json"))
    assert next(i for i in items if i["text"] == "Meet Jason")["mine"] is False
    assert all(i["mine"] for i in items if i["text"] != "Meet Jason")


def test_list_action_items_json(pipeline, note_mcp):
    out = call(note_mcp, "list_action_items", status="all", format="json")
    items = json.loads(out)  # nothing but the array
    meet = next(i for i in items if i["text"] == "Meet Jason")
    conv = conv_of(pipeline, "n1")
    assert meet == {
        "id": meet["id"], "status": "open", "kind": "event", "title": "Meet Jason",
        "mine": True, "explicit": True, "text": "Meet Jason",
        "owner": "Me", "due": "Wednesday", "due_date": "2025-10-15", "due_time": "12:00",
        "duration_minutes": None, "location": None, "list_name": None, "note_title": None,
        "time_unclear": False,
        "conversation_id": conv["id"], "conversation_title": "Plans for the week",
        "conversation_kind": "note", "conversation_at": "2025-10-09T08:53:20+00:00",
        "created_at": meet["created_at"], "timezone": "Europe/Berlin",
        "link": meet["link"], "with": [], "key_points": [], "source": None,
        "written_by_hand": False, "note_link": None}
    assert meet["link"].startswith("http") and meet["link"].endswith(
        f"/view/{conv['id']}?sig={meet['link'].split('sig=')[1].split('#')[0]}#t=1")
    assert meet["created_at"].endswith("+00:00")
    dentist = next(i for i in items if i["kind"] == "event" and i["location"])
    assert (dentist["duration_minutes"], dentist["location"]) == (60, "Main Street")
    assert call(note_mcp, "list_action_items", status="done", format="json") == "[]"
    assert call(note_mcp, "list_action_items", format="xml") == "format must be text or json"


def test_update_action_item_tool(pipeline, note_mcp):
    items = {i["text"]: i for i in json.loads(
        call(note_mcp, "list_action_items", format="json"))}
    meet = items["Meet Jason"]["id"]
    assert call(note_mcp, "update_action_item", item_id=meet, kind="task",
                due_date="2025-10-16", due_time="9:30") == (
        f"Action item #{meet} is now open, task, due 2025-10-16 09:30.")
    assert call(note_mcp, "update_action_item", item_id=meet, status="done") == (
        f"Action item #{meet} is now done, task, due 2025-10-16 09:30.")
    assert call(note_mcp, "update_action_item", item_id=meet, due_date="none",
                due_time="none") == f"Action item #{meet} is now done, task, no due date."
    # bad input comes back as text
    assert "kind must be" in call(note_mcp, "update_action_item", item_id=meet, kind="party")
    assert "due_date must be" in call(note_mcp, "update_action_item", item_id=meet,
                                      due_date="Friday")
    assert "due_time must be" in call(note_mcp, "update_action_item", item_id=meet,
                                      due_time="5pm")
    assert "status must be" in call(note_mcp, "update_action_item", item_id=meet,
                                    status="finished")
    assert "nothing to change" in call(note_mcp, "update_action_item", item_id=meet)
    assert call(note_mcp, "update_action_item", item_id=99_999,
                status="done") == "No action item #99999."


def test_tool_arguments_and_annotations(note_mcp):
    import asyncio

    from mcp import Client

    async def go():
        async with Client(note_mcp) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = asyncio.run(go())
    lst, upd = tools["list_action_items"], tools["update_action_item"]
    assert set(lst.input_schema["properties"]) == {"status", "person", "since", "until",
                                                  "format"}
    assert set(upd.input_schema["properties"]) == {"item_id", "status", "kind", "due_date",
                                                  "due_time", "list_name", "title"}
    assert upd.input_schema.get("required") == ["item_id"]
    assert lst.annotations.read_only_hint is True
    assert upd.annotations.read_only_hint is False and upd.annotations.destructive_hint is False


# ---------------------------------------------------------------------- API
def test_http_action_items(client, pipeline):
    pipeline.summarizer = summarizer(NOTE_ITEMS)
    record(pipeline, "n1", T0, "note")
    rows = client.get("/v1/action-items").json()["action_items"]
    meet = next(r for r in rows if r["text"] == "Meet Jason")
    assert (meet["kind"], meet["due_time"], meet["time_unclear"]) == ("event", "12:00", False)
    assert meet["created_at"].endswith("+00:00")
    url = f"/v1/action-items/{meet['id']}"
    assert client.patch(url, json={"kind": "reminder", "due_time": "08:00"}).status_code == 200
    assert client.patch(url, json={"status": "done"}).status_code == 200
    got = next(r for r in client.get("/v1/action-items?status=all").json()["action_items"]
               if r["id"] == meet["id"])
    assert (got["kind"], got["due_time"], got["status"], got["due_date"]) == (
        "reminder", "08:00", "done", "2025-10-15")
    assert client.patch(url, json={"due_date": None}).status_code == 200
    assert client.patch(url, json={"kind": "party"}).status_code == 400
    assert client.patch(url, json={}).status_code == 400
    assert client.patch("/v1/action-items/99999", json={"status": "done"}).status_code == 404
