"""Note items: "create a note ..." / "add to my hardware notes ..." in a voice note becomes
an action item of kind "note" (a title, the text, and the notes to add it to), which
Odysseus files into its notes. Voice notes and explicit commands only."""

from __future__ import annotations

import json

import pytest
from conftest import fake_llm_transport
from test_mcp import call
from test_voice_notes import conv_of, record

from convo_server.llm import NOTE_ACTIONS_SYSTEM, LLMClient, Summarizer
from convo_server.mcp_server import build_mcp
from convo_server.store import Segment, clean_notes_name
from convo_server.transcribe import TranscriptPiece

T0 = 1_760_000_000.0  # Thursday 2025-10-09

BATTERY = {"text": "Battery dimensions are 13 mm x 14 mm x 3 mm", "kind": "note",
           "title": "Battery dimensions", "at_seconds": 0}
SCREEN = {"text": "The screen is 1.4 inch", "kind": "note", "title": "Screen size",
          "list_name": "my hardware notes", "at_seconds": 0}
TASK = {"text": "Call the plumber", "kind": "task", "owner": "Me", "at_seconds": 0}


def summarizer(items: list[dict]) -> Summarizer:
    summary = {"title": "Battery", "summary": "s", "key_points": [], "people": [],
               "topics": [], "facts": [], "action_items": items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    return Summarizer(llm, "Me", timezone="Europe/Berlin")


def note_items(items: list[dict], said: str, kind: str = "note") -> list[dict]:
    out = summarizer(items).summarize([Segment(T0, T0 + 5, said, "Me", True)], kind=kind)
    return out["action_items"]


# --------------------------------------------------------------------- summarizing
def test_create_a_note():
    (item,) = note_items([BATTERY], "Create a note battery dimensions are 13mm x 14mm x 3mm")
    assert (item["kind"], item["text"], item["note_title"], item["list_name"]) == (
        "note", "Battery dimensions are 13 mm x 14 mm x 3 mm", "Battery dimensions", None)
    assert (item["due"], item["due_date"], item["due_time"], item["owner"]) == (
        None, None, None, None)


def test_add_to_named_notes():
    (item,) = note_items([SCREEN], "Add to my hardware notes, the screen is 1.4 inch")
    assert (item["text"], item["note_title"], item["list_name"]) == (
        "The screen is 1.4 inch", "Screen size", "hardware")


@pytest.mark.parametrize("text,list_name,want", [
    # the model left the command words in: taken out in code
    ("Create a note battery dimensions are 13mm", None, ("Battery dimensions are 13mm", None)),
    ("Note that the wifi password is on the router", None,
     ("The wifi password is on the router", None)),
    ("Make a new note: buy a 3 mm drill bit", None, ("Buy a 3 mm drill bit", None)),
    # the notes named in the text, not in list_name
    ("Add to my hardware notes: the screen is 1.4 inch", None,
     ("The screen is 1.4 inch", "hardware")),
    ("The screen is 1.4 inch, add it to the car notes", None,
     ("The screen is 1.4 inch", "car")),
    # "my notes" names no notes: a note of its own
    ("Battery is 3 mm", "my notes", ("Battery is 3 mm", None)),
    ("Battery is 3 mm", "Hardware Notes", ("Battery is 3 mm", "Hardware")),
])
def test_note_text_and_name_cleanup(text, list_name, want):
    (item,) = note_items([{"text": text, "kind": "note", "list_name": list_name}],
                         f"note {text}")
    assert (item["text"], item["list_name"]) == want


def test_explicit_commands_only():
    # no "note" said: whatever the model thinks, no note is made
    items = note_items([BATTERY, TASK], "Battery dimensions are 13 by 14 by 3, call the plumber")
    assert [(i["kind"], i["text"]) for i in items] == [("task", "Call the plumber")]


def test_conversations_make_no_notes():
    (item,) = note_items([BATTERY], "Create a note battery dimensions", kind="casual")
    assert item["kind"] == "task" and item["note_title"] is None


def test_note_prompt_explains_notes():
    for words in ('kind "note" ONLY', "create a note", "add to my hardware notes",
                  'list_name "hardware"', "title"):
        assert words in NOTE_ACTIONS_SYSTEM


@pytest.mark.parametrize("said,want", [
    ("hardware", "hardware"), ("my Hardware notes", "Hardware"), ("the car note", "car"),
    ("notes", None), ("my notes", None), ("", None), (None, None), ("x" * 41, None),
])
def test_clean_notes_name(said, want):
    assert clean_notes_name(said) == want


# ------------------------------------------------------------------- storage, MCP, API
@pytest.fixture
def stored(pipeline):
    def transcribe(audio, prompt=""):
        return [TranscriptPiece(0.0, audio.size / 16000,
                                "Create a note battery dimensions are 13mm x 14mm x 3mm. "
                                "Add to my hardware notes the screen is 1.4 inch. "
                                "Call the plumber.")]
    pipeline.transcriber.transcribe = transcribe
    pipeline.settings.timezone = "Europe/Berlin"
    pipeline.summarizer = summarizer([BATTERY, SCREEN, TASK])
    record(pipeline, "n1", T0, "note")
    return pipeline


def test_json_lists_notes_with_title_and_name(stored):
    mcp = build_mcp(stored.store, stored.settings)
    rows = {r["text"]: r for r in json.loads(call(mcp, "list_action_items", format="json"))}
    battery, screen = rows["Battery dimensions are 13 mm x 14 mm x 3 mm"], rows[
        "The screen is 1.4 inch"]
    assert (battery["kind"], battery["note_title"], battery["list_name"],
            battery["conversation_kind"]) == ("note", "Battery dimensions", None, "note")
    assert (screen["note_title"], screen["list_name"]) == ("Screen size", "hardware")
    assert rows["Call the plumber"]["note_title"] is None


def test_notes_are_no_todos_in_text_digest_and_http(stored, client):
    mcp = build_mcp(stored.store, stored.settings)
    text = call(mcp, "list_action_items")
    assert "Call the plumber" in text and "13 mm" not in text and "screen" not in text
    rows = client.get("/v1/action-items").json()["action_items"]
    assert [r["text"] for r in rows] == ["Call the plumber"]
    rows = client.get("/v1/action-items?include_notes=true").json()["action_items"]
    assert {r["kind"] for r in rows} == {"note", "task"}
    # the digest model sees only the to-do
    conv = conv_of(stored, "n1")
    log: list = []
    llm = LLMClient("http://x/v1", "m", transport=fake_llm_transport(log=log))
    Summarizer(llm, "Me").digest("2025-10-09", [conv])
    (body,) = log
    sent = json.loads(body["messages"][-1]["content"])
    assert sent[0]["action_items"] == ["Call the plumber"]


def test_status_and_kind_changes(stored):
    item = next(r for r in stored.store.list_action_items() if r["kind"] == "note")
    assert stored.store.update_action_item(item["id"], status="done")["status"] == "done"
    # a wrongly made note can become a to-do; nothing becomes a note
    assert stored.store.update_action_item(item["id"], kind="task")["kind"] == "task"
    with pytest.raises(ValueError):
        stored.store.update_action_item(item["id"], kind="note")


# ---------------------------------------------------------- commands the model missed
def test_missed_command_is_made_a_note_in_code():
    (item,) = note_items([], "Note that the router admin page is at 192.168.1.1.")
    assert (item["kind"], item["text"], item["note_title"], item["list_name"]) == (
        "note", "The router admin page is at 192.168.1.1", None, None)
    assert item["source_at"] == T0 and item["due_time"] is None


def test_command_taken_for_a_task_becomes_the_note():
    task = {"text": "The screen is 1.4 inch", "kind": "task", "at_seconds": 0}
    (item,) = note_items([task], "Add to my hardware notes the screen is 1.4 inch")
    assert (item["kind"], item["text"], item["list_name"]) == (
        "note", "The screen is 1.4 inch", "hardware")


def test_fallback_keeps_other_items_and_model_notes():
    milk = {"text": "Buy milk", "kind": "task", "at_seconds": 0}
    garage = {"text": "The garage code is 4417", "kind": "note", "title": "Garage code",
              "at_seconds": 0}
    items = note_items([milk, garage],
                       "Remember to buy milk. And create a note the garage code is 4417.")
    assert [(i["kind"], i["text"], i["note_title"]) for i in items] == [
        ("task", "Buy milk", None), ("note", "The garage code is 4417", "Garage code")]
    # a task with a date is never taken for the note
    dated = {"text": "Call the plumber", "kind": "task", "due": "Friday", "at_seconds": 0}
    items = note_items([dated], "Note that I should call the plumber. Call the plumber Friday")
    assert sorted(i["kind"] for i in items) == ["note", "task"]
