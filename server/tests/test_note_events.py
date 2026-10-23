"""A voice note that names an appointment with a day and a time ("Meeting with the
Meshtastic team Tuesday 11 a.m.") gives an event even when the model lists no item for
it, so the hub puts it on the calendar."""

from __future__ import annotations

from conftest import fake_llm_transport

from convo_server.llm import NOTE_ACTIONS_SYSTEM, LLMClient, Summarizer
from convo_server.store import Segment

T0 = 1_760_000_000.0  # Thursday 2025-10-09, 10:53 in Berlin
TUESDAY = "2025-10-14"


def items(said: str, model_items: list[dict], kind: str | None = "note") -> list[dict]:
    summary = {"title": "Meeting", "summary": said, "key_points": [], "people": [],
               "topics": [], "facts": [], "action_items": model_items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    summarizer = Summarizer(llm, "Me", timezone="Europe/Berlin", min_words=1)
    return summarizer.summarize([Segment(T0, T0 + 5, said, "Me", True)],
                                kind=kind)["action_items"]


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
    assert (item["text"], item["due_date"], item["due_time"]) == (
        "Meet the Meshtastic team", TUESDAY, "11:00")


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
