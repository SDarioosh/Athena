"""A voice note that names an appointment with a day and a time ("Meeting with the
Meshtastic team Tuesday 11 a.m.") gives an event even when the model lists no item for
it, so the hub puts it on the calendar."""

from __future__ import annotations

from athena.domain import Segment
from athena.summarize.prompts import ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM
from tests.conftest import make_summarizer

T0 = 1_760_000_000.0  # Thursday 2025-10-09, 10:53 in Berlin
TUESDAY = "2025-10-14"


def summarize(said: str, model_items: list[dict], kind: str | None = "note",
              facts: list[dict] | None = None) -> dict:
    summary = {"title": "Meeting", "summary": said, "key_points": [], "people": [],
               "topics": [], "facts": facts or [], "action_items": model_items}
    summarizer = make_summarizer(summary, min_words=1)
    return summarizer.summarize([Segment(T0, T0 + 5, said, "Me", True)], kind=kind)


def items(said: str, model_items: list[dict], kind: str | None = "note") -> list[dict]:
    return summarize(said, model_items, kind)["action_items"]


def test_fragment_without_an_item_becomes_an_event():
    (item,) = items("Meeting with Meshtastic team Tuesday 11 a.m.", [])
    assert (item["kind"], item["text"], item["title"]) == (
        "event", "Meeting with Meshtastic team", "Meeting with Meshtastic team")
    assert (item["due_date"], item["due_time"], item["time_unclear"]) == (
        TUESDAY, "11:00", False)
    assert item["explicit"] is True and item["owner"] == "Me"
    assert item["source_at"] == T0


def test_added_event_has_the_fields_of_a_model_item():
    (added,) = items("Meeting with Meshtastic team Tuesday 11 a.m.", [])
    (model,) = items("Meet Jason on Friday at 3 pm", [
        {"text": "Meet Jason", "kind": "event", "due": "Friday", "time": "3 pm",
         "at_seconds": 0}])
    assert set(added) == set(model)


def test_undated_model_task_gets_the_day_and_time():
    (item,) = items("Meeting with Meshtastic team Tuesday 11 a.m.", [
        {"text": "Attend meeting with Meshtastic team", "kind": "task", "at_seconds": 0}])
    assert (item["kind"], item["text"], item["due_date"], item["due_time"]) == (
        "event", "Attend meeting with Meshtastic team", TUESDAY, "11:00")
    assert item["explicit"] is True


def test_model_event_is_not_doubled():
    (item,) = items("Meeting with Meshtastic team Tuesday 11 a.m.", [
        {"text": "Meet the Meshtastic team", "kind": "event", "due": "Tuesday",
         "time": "11 a.m.", "at_seconds": 0}])
    assert (item["text"], item["title"], item["due_date"], item["due_time"]) == (
        "Meeting with Meshtastic team", "Meeting with Meshtastic team", TUESDAY, "11:00")


def test_reworded_model_event_on_the_same_line_is_not_doubled():
    (item,) = items("Lunch with Jason Wednesday at noon.", [
        {"text": "Meet Jason", "kind": "event", "due": "Wednesday", "time": "noon",
         "at_seconds": 0}])
    assert item["text"] == "Meet Jason"


def test_other_task_on_the_same_line_keeps_no_date():
    milk, meeting = items("Buy milk. Meeting with Bob Tuesday at 3 pm.", [
        {"text": "Buy milk", "kind": "task", "at_seconds": 0}])
    assert (milk["text"], milk["kind"], milk["due_date"]) == ("Buy milk", "task", None)
    assert (meeting["text"], meeting["kind"], meeting["due_date"], meeting["due_time"]) == (
        "Meeting with Bob", "event", TUESDAY, "15:00")


def test_needs_a_day_a_time_and_an_appointment():
    assert items("Meeting with the team on Tuesday.", []) == []  # no time
    assert items("Meeting with the team at 3 pm.", []) == []  # no day
    assert items("Tuesday at 11 am the parcel arrives.", []) == []  # no appointment


def test_past_appointments_are_left_out():
    assert items("I had a meeting with Bob on Tuesday at 3 pm.", []) == []
    assert items("Met the landlord Monday at 9 am, all fine.", []) == []


def test_conversations_are_left_alone():
    assert items("Meeting with Meshtastic team Tuesday 11 a.m.", [], kind=None) == []


def test_prompt_shows_a_verbless_appointment():
    assert "meeting with the design team Tuesday 11 a.m." in NOTE_ACTIONS_SYSTEM
    assert "is no note" in NOTE_ACTIONS_SYSTEM


# ------------------------------------------------------------- the meeting's name
def test_meet_x_is_named_as_said():
    (item,) = items("Meeting with the board Friday at 2 pm.", [
        {"text": "Meet with the board", "title": "Meet the board", "kind": "event",
         "due": "Friday", "time": "2 pm", "at_seconds": 0}])
    assert (item["text"], item["title"]) == ("Meeting with the board", "Meeting with the board")


def test_meet_x_in_a_conversation_is_named_as_said():
    (item,) = items("We have a meeting with Dana on Friday at 2 pm.", [
        {"text": "Meet Dana", "kind": "event", "due": "Friday", "time": "2 pm",
         "at_seconds": 0}], kind=None)
    assert item["text"] == "Meeting with Dana"


def test_meet_is_kept_when_the_user_said_meet():
    (item,) = items("I'll meet Sam at the station Friday at 2 pm.", [
        {"text": "Meet Sam at the station", "kind": "event", "due": "Friday",
         "time": "2 pm", "at_seconds": 0}])
    assert item["text"] == "Meet Sam at the station"


def test_meeting_with_someone_else_is_not_used():
    (meet, _) = items("Meet Sam Friday at 2 pm. Meeting with Bob Tuesday at 3 pm.", [
        {"text": "Meet Sam", "kind": "event", "due": "Friday", "time": "2 pm",
         "at_seconds": 0}])
    assert meet["text"] == "Meet Sam"


def test_own_title_is_kept():
    (item,) = items("Meeting with Meshtastic team Tuesday 11 a.m.", [
        {"text": "Meet the Meshtastic team", "title": "Meshtastic sync", "kind": "event",
         "due": "Tuesday", "time": "11 a.m.", "at_seconds": 0}])
    assert (item["text"], item["title"]) == ("Meeting with Meshtastic team", "Meshtastic sync")


def test_prompts_name_events_as_said():
    assert '-> text "Meeting with Jason", kind event' in NOTE_ACTIONS_SYSTEM
    assert '-> text "Meeting with Jason", kind event' in ACTIONS_SYSTEM
    assert "Meet Jason\" ->" not in ACTIONS_SYSTEM


# ------------------------------------------------------------- facts that repeat it
MESHTASTIC_FACT = {"subject": "Me", "at_seconds": 0,
                   "fact": "Me has a meeting with the Meshtastic team on Tuesday at 11 a.m."}


def test_fact_that_repeats_the_event_is_dropped():
    out = summarize("Meeting with Meshtastic team Tuesday 11 a.m.", [], facts=[
        MESHTASTIC_FACT,
        {"subject": "Me", "fact": "Me works on Meshtastic firmware.", "at_seconds": 0}])
    assert [f["fact"] for f in out["facts"]] == ["Me works on Meshtastic firmware."]
    assert out["action_items"][0]["kind"] == "event"


def test_fact_that_repeats_the_event_is_dropped_in_a_conversation():
    out = summarize("We have a meeting with Dana on Friday at 2 pm.", [
        {"text": "Meet Dana", "kind": "event", "due": "Friday", "time": "2 pm",
         "at_seconds": 0}], kind=None, facts=[
        {"subject": "Me", "fact": "Me has a meeting with Dana on Friday at 2 pm.",
         "at_seconds": 0}])
    assert out["facts"] == []


def test_dated_fact_without_a_weekday_or_time_stays():
    out = summarize("Lunch with Alice Friday at noon. Her wedding is on June 14.", [], facts=[
        {"subject": "Alice", "fact": "Alice's wedding is on June 14.", "at_seconds": 0}])
    assert [f["fact"] for f in out["facts"]] == ["Alice's wedding is on June 14."]


def test_fact_stays_without_an_event():
    out = summarize("Talked about Meshtastic.", [], facts=[MESHTASTIC_FACT])
    assert [f["fact"] for f in out["facts"]] == [MESHTASTIC_FACT["fact"]]
