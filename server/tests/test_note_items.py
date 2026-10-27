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


def summarizer(items: list[dict], said: str = "") -> Summarizer:
    # no summary by default: a lone note keeps its own words (see test_lone_note_*)
    summary = {"title": "Battery", "summary": said, "key_points": [], "people": [],
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


def test_renaming_a_note_renames_its_note_title(stored):
    """QC S9: a note renamed with update_action_item kept its old note_title, the name
    the hub files the note under."""
    mcp = build_mcp(stored.store, stored.settings)
    rows = stored.store.list_action_items(notes=True)
    note = next(r for r in rows if r["note_title"] == "Battery dimensions")
    task = next(r for r in rows if r["kind"] == "task")
    call(mcp, "update_action_item", item_id=note["id"], title="Battery size")
    call(mcp, "update_action_item", item_id=task["id"], title="Plumber")
    rows = {r["id"]: r for r in json.loads(call(mcp, "list_action_items", format="json"))}
    assert (rows[note["id"]]["title"], rows[note["id"]]["note_title"]) == (
        "Battery size", "Battery size")
    assert (rows[task["id"]]["title"], rows[task["id"]]["note_title"]) == ("Plumber", None)


# ---------------------------------------------------------- commands the model missed
def test_missed_command_is_made_a_note_in_code():
    (item,) = note_items([], "Note that the router admin page is at 192.168.1.1.")
    # the voice note's only item: titled like the voice note
    assert (item["kind"], item["text"], item["note_title"], item["list_name"]) == (
        "note", "The router admin page is at 192.168.1.1", "Battery", None)
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


def test_shopping_list_items_stay_out_of_the_digest():
    conv = {"id": "c1", "started_at": 1_760_000_000, "title": "Errands", "summary": "s",
            "key_points": [], "people": [], "action_items": [
                {"text": "Milk", "status": "open", "kind": "task", "list_name": "shopping"},
                {"text": "Eggs", "status": "open", "kind": "task", "list_name": "Grocery List"},
                {"text": "Call the bank", "status": "open", "kind": "task", "list_name": "work"},
                {"text": "Book flights", "status": "open", "kind": "task", "list_name": None}]}
    log: list = []
    llm = LLMClient("http://x/v1", "m", transport=fake_llm_transport(log=log))
    Summarizer(llm, "Me").digest("2025-10-09", [conv])
    Summarizer(llm, "Me", digest_skip_lists="").digest("2025-10-09", [conv])
    first, second = (json.loads(b["messages"][-1]["content"])[0]["action_items"] for b in log)
    assert first == ["Call the bank", "Book flights"]
    assert second == ["Milk", "Eggs", "Call the bank", "Book flights"]


# --------------------------------------------- the command and the note in two sentences
@pytest.mark.parametrize("said,item,notes", [
    # the model takes the measurement for a task ("by 14" is no deadline)
    ("Create a note. Battery dimensions are 13 mm by 14 mm by 3 mm.",
     {"text": "Battery dimensions are 13 mm by 14 mm by 3 mm"}, None),
    ("Add to my hardware notes. The screen is 1.4 inch.", {"text": "The screen is 1.4 inch"},
     "hardware"),
    ("Add this to my hardware notes. The screen is 1.4 inch.",
     {"text": "The screen is 1.4 inch"}, "hardware"),
])
def test_note_in_the_next_sentence(said, item, notes):
    (got,) = note_items([item], said)
    assert (got["kind"], got["text"], got["list_name"], got["due_time"], got["explicit"]) == (
        "note", item["text"], notes, None, True)


def test_no_note_from_the_command_alone():
    items = note_items([], "Add to my hardware notes. The screen is 1.4 inch.")
    assert [(i["kind"], i["text"], i["list_name"]) for i in items] == [
        ("note", "The screen is 1.4 inch", "hardware")]


# ------------------------------------------- a sentence the transcriber split over lines
def lines_items(items: list[dict], lines: list[str]) -> list[dict]:
    segments = [Segment(T0 + 4 * i, T0 + 4 * i + 4, text, "Me", True)
                for i, text in enumerate(lines)]
    return summarizer(items).summarize(segments, kind="note")["action_items"]


def test_note_read_on_into_the_next_line():
    (item,) = lines_items([], ["Make a note that the hub has a bug where a",
                               "speaker gets the wrong name when two people talk.",
                               "That's all."])
    assert (item["kind"], item["text"]) == (
        "note", "The hub has a bug where a speaker gets the wrong name when two people talk")
    assert item["source_at"] == T0 and item["explicit"] is True
    assert (item["note_title"], item["title"]) == ("Battery", "Battery")


def test_a_finished_line_is_not_read_on():
    items = lines_items([], ["Note that the gate code is 4417.", "Call the plumber"])
    assert [(i["kind"], i["text"]) for i in items] == [("note", "The gate code is 4417")]


def test_twin_task_with_more_words_keeps_them():
    task = {"text": "The hub has a bug where a speaker gets the wrong name", "kind": "task",
            "at_seconds": 0}
    (item,) = note_items([task], "Make a note that the hub has a bug where a speaker")
    assert (item["kind"], item["text"]) == (
        "note", "The hub has a bug where a speaker gets the wrong name")


def test_several_notes_keep_their_own_titles():
    items = note_items([], "Note that the gate code is 4417. Note that the wifi is on the router.")
    assert [i["note_title"] for i in items] == [None, None]


# ------------------------------------------------ a voice note that is only one note
SUMMARY = ("A speaker gets the wrong name in the hub when two people talk over each "
           "other, a diarization bug.")


def test_lone_note_is_the_summary():
    out = summarizer([], SUMMARY).summarize(
        [Segment(T0, T0 + 4, "Make a note that the hub has a bug where a", "Me", True),
         Segment(T0 + 4, T0 + 8, "speaker, um, gets the wrong name.", "Me", True)], kind="note")
    (item,) = out["action_items"]
    assert (item["kind"], item["text"], item["note_title"], item["title"]) == (
        "note", SUMMARY, "Battery", "Battery")
    assert item["source_at"] == T0 and item["explicit"] is True


def test_named_notes_and_several_notes_keep_their_words():
    said = "Add to my hardware notes, the screen is 1.4 inch"
    (item,) = summarizer([SCREEN], "Specs of the screen.").summarize(
        [Segment(T0, T0 + 5, said, "Me", True)], kind="note")["action_items"]
    assert (item["text"], item["list_name"]) == ("The screen is 1.4 inch", "hardware")
    items = summarizer([], "Gate code and wifi.").summarize(
        [Segment(T0, T0 + 5, "Note that the gate code is 4417. Note that the wifi is on the "
                 "router.", "Me", True)], kind="note")["action_items"]
    assert [i["text"] for i in items] == ["The gate code is 4417", "The wifi is on the router"]


def test_lone_note_with_tasks_keeps_its_words():
    items = summarizer([TASK], "Plumber and gate code.").summarize(
        [Segment(T0, T0 + 5, "Note that the gate code is 4417. Call the plumber.", "Me",
                 True)], kind="note")["action_items"]
    assert sorted((i["kind"], i["text"]) for i in items) == [
        ("note", "The gate code is 4417"), ("task", "Call the plumber")]


# ---------------------------------- the key points of a lone note in JSON (MCP 0.15.0)
T4_SUMMARY = ("Recorded physical specifications for the AMOLED T4-S3 device, including "
              "screen, overall thickness, and PCB dimensions.")
T4_POINTS = ["Screen: 49mm x 60mm", "Overall device thickness: ~8mm", "PCB: 39mm x 52mm",
             "Distance between PCB and screen: 6.5mm"]


def test_lone_note_json_has_the_voice_notes_key_points(pipeline, stored):
    def transcribe(audio, prompt=""):
        return [TranscriptPiece(0.0, audio.size / 16000,
                                "Make a note, AMOLED T4-S3 dimensions. The screen is 49mm by "
                                "60mm. Thickness of the overall device is about 8mm.")]
    pipeline.transcriber.transcribe = transcribe
    summary = {"title": "AMOLED T4-S3 Dimensions", "summary": T4_SUMMARY,
               "key_points": T4_POINTS, "people": [], "topics": [], "facts": [],
               "action_items": []}
    pipeline.summarizer = Summarizer(
        LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary)),
        "Me", timezone="Europe/Berlin")
    record(pipeline, "n2", T0 + 3600, "note")
    mcp = build_mcp(pipeline.store, pipeline.settings)
    rows = {r["text"]: r for r in json.loads(call(mcp, "list_action_items", format="json"))}
    lone = rows[T4_SUMMARY]
    assert (lone["kind"], lone["note_title"], lone["key_points"]) == (
        "note", "AMOLED T4-S3 Dimensions", T4_POINTS)
    # several notes, named notes and to-dos keep their own words: no key points
    assert rows["Battery dimensions are 13 mm x 14 mm x 3 mm"]["key_points"] == []
    assert rows["The screen is 1.4 inch"]["key_points"] == []
    assert rows["Call the plumber"]["key_points"] == []
