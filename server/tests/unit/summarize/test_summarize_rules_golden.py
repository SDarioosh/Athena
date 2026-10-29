"""The summarizer's rules on many fictional recordings: every request sent and every
result, byte for byte, against ``tests/unit/summarize/fixtures/summarize_rules.json``.

``test_summarize_requests.py`` pins the prompts and a few broad scenarios; this file aims
at the rules that run after the model answers (spoken commands, list headers, notes and
their places, events, passing facts, errands, intentions, the fact review, duplicates,
voice notes split over several calls, ``link_facts``, ``remove_facts``), and at the way
the rules read the transcript lines (several lines in one second, a line broken
mid-sentence, odd speaker names and texts). A corpus of single-sentence voice notes runs
each sentence with no model item, so the rules alone decide.

A change to a rule that is meant to change results rewrites the file:

    UPDATE_SNAPSHOTS=1 pytest tests/unit/summarize/test_summarize_rules_golden.py
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from athena.domain import Segment
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from tests.unit.summarize.test_summarize_requests import snapshot

SNAPSHOT = Path(__file__).parent / "fixtures" / "summarize_rules.json"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

# Monday 2025-10-06 09:00 UTC (11:00 in Berlin)
ORIGIN = 1759741200.0

Items = list[dict[str, Any]] | Callable[[str], list[dict[str, Any]]]


def transport(sent: list[str], *, items: Items = (), facts: Items = (),
              notes: dict[str, Any] | None = None, kind: str = "casual",
              discard: str = "none", scores: Callable[[dict[str, Any]], int] | None = None,
              review_fails: bool = False, links: list[dict[str, Any]] | None = None,
              forget: dict[str, Any] | None = None) -> httpx.MockTransport:
    """Fake llama-server: records each request body as sent, answers by schema name.
    ``items`` and ``facts`` may be a function of the user message (one part of a split
    transcript), ``scores`` a function of the fact the review is shown."""
    notes = notes or {"title": "Voice note", "summary": "", "key_points": [],
                      "people": [], "topics": []}

    def pick(x: Items, user: str) -> list[dict[str, Any]]:
        return x(user) if callable(x) else list(x)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        user = body["messages"][-1]["content"]
        if name == "FactReview":
            if review_fails:
                return httpx.Response(500, text="review down")
            asked = json.loads(user)["facts"]
            content: Any = {"scores": [{"number": f["number"],
                                        "score": scores(f) if scores else 5}
                                       for f in asked]}
        else:
            content = {
                "Triage": {"discard": discard != "none", "reason": discard, "kind": kind},
                "SummaryNotes": notes,
                "ActionItemList": {"action_items": pick(items, user)},
                "FactList": {"facts": pick(facts, user)},
                "FactLinks": {"links": links or []},
                "ForgetRewrite": forget or {},
                "DailyDigest": {"headline": "A day", "summary": "Plans.", "highlights": [],
                                "open_action_items": [], "people": []},
            }[name]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(content)}}]})

    return httpx.MockTransport(handler)


def summarizer(sent: list[str], *, owner: str = "Me", tz: str = "Europe/Berlin",
               max_chars: int = 120_000, min_words: int = 3, max_facts: int = 3,
               fact_min_score: int = 4, digest_skip_lists: str = "Shopping,Groceries,Grocery",
               **fake: Any) -> Summarizer:
    llm = LLMClient("http://llm.test/v1", "test-model", transport=transport(sent, **fake))
    return Summarizer(llm, owner, max_chars_per_call=max_chars, timezone=tz,
                      min_words=min_words, max_facts=max_facts,
                      fact_min_score=fact_min_score, digest_skip_lists=digest_skip_lists)


def segs(lines: list[tuple[Any, ...]], origin: float = ORIGIN) -> list[Segment]:
    """(offset, speaker, text[, kind]) -> segments; "Me" is the owner."""
    out = []
    for line in lines:
        at, speaker, text = line[:3]
        kind = line[3] if len(line) > 3 else "speech"
        out.append(Segment(origin + at, origin + at + 3, text, speaker,
                           is_owner=speaker == "Me", kind=kind))
    return out


def note_lines(*said: str, step: int = 5) -> list[tuple[Any, ...]]:
    return [(step * i, "Me", s) for i, s in enumerate(said)]


def known_alice(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"subject": "Alice", "fact": "Alice works at a bakery in Porto."},
            {"subject": "Me", "fact": "Me prefers green tea."}]


def known_fails(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raise RuntimeError("store down")


# ---------------------------------------------------------------------------- scenarios
# name -> (summarize kwargs, fake model kwargs, lines)
SCENARIOS: dict[str, tuple[dict[str, Any], dict[str, Any], list[tuple[Any, ...]]]] = {}


def scenario(name: str, lines: list[tuple[Any, ...]], *, kind: str | None = "note",
             call: dict[str, Any] | None = None, **fake: Any) -> None:
    SCENARIOS[name] = ({"kind": kind, **(call or {})}, fake, lines)


# --- voice notes: spoken to-do commands
scenario("note_todo_missed_by_model", note_lines(
    "Make a to-do to figure out how renewing a fishing permit works", "for Bruno."))
scenario("note_todo_reworded_without_date", note_lines(
    "Um, make a to-do to get eggs tomorrow."),
    items=[{"text": "Get eggs", "kind": "task", "owner": "Me", "at_seconds": 0}])
scenario("note_remind_me_at_five", note_lines("Remind me at 5 to call Mom."),
         items=[{"text": "Call Mom", "kind": "task", "owner": "Me", "at_seconds": 0}])
scenario("note_remind_me_no_time_midnight", note_lines("Remind me to call the bank."),
         items=[{"text": "Call the bank", "kind": "reminder", "owner": "Me",
                 "time": "00:00", "at_seconds": 0}])
scenario("note_remind_me_said_midnight", note_lines("Remind me at midnight to call Ana."),
         items=[{"text": "Call Ana", "kind": "reminder", "owner": "Me", "time": "00:00",
                 "at_seconds": 0}])
scenario("note_remind_me_model_kept_date", note_lines(
    "Remind me on Friday to pay the electricity bill."),
    items=[{"text": "Pay the electricity bill", "kind": "reminder", "owner": "Me",
            "due": "Thursday", "due_date": "2025-10-09", "at_seconds": 0}])
scenario("note_remind_me_about", note_lines("Remind me about the dentist on Monday."))
scenario("note_todo_for_tomorrow", note_lines("Make a to-do for tomorrow to call the vet."),
         items=[{"text": "Call the vet", "kind": "task", "owner": "Me", "at_seconds": 0}])
scenario("note_todo_with_fillers", note_lines(
    "Um, so, make a to-do to... uh... fix the leaking tap, thanks."))
scenario("note_todo_hedged_request", note_lines(
    "Can you please make a to-do to call the bank?", "I'd like to add a task to email Ana."))
scenario("note_two_commands_one_sentence", note_lines(
    "Make a to-do to get eggs and make a note that the car is broken."),
    items=[{"text": "Get eggs and note that the car is broken", "kind": "note",
            "at_seconds": 0}])
scenario("note_model_joined_note_and_todo", note_lines(
    "Make a note that the car is broken.", "Make a to-do to call the mechanic."),
    items=[{"text": "The car is broken, to-do call the mechanic", "kind": "note",
            "at_seconds": 0}])

# --- list headers and named lists
scenario("note_list_header_weekend", note_lines(
    "Make a to-do list for the weekend.", "Clean the garage.", "Mow the lawn.",
    "Okay, that's it.", "I'll grab the keys real quick."),
    items=[{"text": "Clean the garage", "kind": "task", "owner": "Me", "at_seconds": 5}])
scenario("note_bare_list_header", note_lines("Shopping list: eggs, milk and bread."))
scenario("note_list_header_ends_on_command", note_lines(
    "Make a shopping list.", "Eggs.", "Milk.", "Make a note that the fridge hums.",
    "Bread."))
scenario("note_list_header_long_sentence_ends_it", note_lines(
    "Make a to-do list.", "Get eggs.",
    "Then we talked for a long time about the summer holiday and the beach house.",
    "Fix the car."))
scenario("note_list_commands", note_lines(
    "Add oat milk to my groceries list for Friday.", "Put call the bank on my work to-do list.",
    "Add to the shopping list: oat milk.", "Add salt and pepper to my shopping list."),
    items=[{"text": "Call the bank", "kind": "task", "owner": "Me", "at_seconds": 5}])
scenario("note_model_list_split_lines", note_lines("Get eggs, milk and bread for the shop."),
         items=[{"text": "Eggs, milk and bread", "kind": "task", "list_name": "shopping",
                 "owner": "Me", "at_seconds": 0},
                {"text": "Salt and pepper", "kind": "task", "list_name": "shopping",
                 "owner": "Me", "at_seconds": 0},
                {"text": "Buy a new garden hose, fix the shed door and paint it",
                 "kind": "task", "list_name": "garden", "owner": "Me", "at_seconds": 0}])
scenario("note_list_command_with_more_than_day", note_lines(
    "Add the parcel to my to-do list for the neighbour downstairs."))

# --- notes and their places
scenario("note_in_place_model_task", note_lines(
    "Make a note in voice recorder that the case needs a new hinge."),
    items=[{"text": "Case needs a new hinge", "kind": "task", "owner": "Me",
            "at_seconds": 0}])
scenario("note_command_only_next_sentence", note_lines(
    "Create a note.", "The battery is 13 mm wide."))
scenario("note_named_notes_phrase", note_lines(
    "Add to my car notes: tyre pressure is 2.4 bar."),
    items=[{"text": "Tyre pressure is 2.4 bar", "kind": "note", "at_seconds": 0}])
scenario("note_model_note_without_note_word", note_lines("The gate code is 4512."),
         items=[{"text": "The gate code is 4512", "kind": "note", "at_seconds": 0}])
scenario("note_lone_note_takes_summary", note_lines(
    "Make a note that the boiler pressure is low", "and the valve drips."),
    items=[{"text": "Boiler pressure is low", "kind": "note", "at_seconds": 0}],
    notes={"title": "Boiler check", "summary": "The boiler pressure is low and the "
           "valve  drips.", "key_points": ["Low pressure"], "people": [], "topics": []})
scenario("note_lone_note_in_named_notes_keeps_words", note_lines(
    "Add to my house notes: the boiler pressure is low."),
    notes={"title": "Boiler", "summary": "Boiler pressure is low.", "key_points": [],
           "people": [], "topics": []})
scenario("note_note_with_time_cleared", note_lines(
    "Make a note that the shop opens at 9 on Friday."),
    items=[{"text": "The shop opens at 9 on Friday", "kind": "note", "due": "Friday",
            "time": "9 am", "owner": "Me", "at_seconds": 0}])
scenario("note_model_wrote_more", note_lines("Note: the car is broken."),
         items=[{"text": "The car is broken, the clutch slips in third gear",
                 "kind": "task", "owner": "Me", "at_seconds": 0}])

# --- events
scenario("note_event_fragment", note_lines(
    "Meeting with the Meshtastic team Tuesday 11 a.m.",
    "Lunch with Jason on Wednesday at 12 pm, make a note to bring the slides."))
scenario("note_event_twin_dated", note_lines("Dentist appointment Friday at 3 pm."),
         items=[{"text": "Dentist appointment", "kind": "task", "owner": "Me",
                 "at_seconds": 0}])
scenario("note_event_same_day_same_line", note_lines("Coffee with Ana tomorrow at 10."),
         items=[{"text": "Meet Ana", "kind": "event", "owner": "Me",
                 "due_date": "2025-10-07", "time": "10 am", "at_seconds": 0}])
scenario("note_event_past_tense", note_lines("I had a meeting with Bob on Monday at 9."))
scenario("note_meet_named_as_said", note_lines(
    "Meeting with the design team on Thursday at 2 pm."),
    items=[{"text": "Meet the design team", "kind": "event", "owner": "Me",
            "title": "Meet the design team", "due": "Thursday", "time": "2 pm",
            "at_seconds": 0}],
    facts=[{"subject": "Me", "fact": "Me has a meeting with the design team on Thursday "
            "at 2 pm.", "kind": "plan", "at_seconds": 0},
           {"subject": "Me", "fact": "Me's design team is in Leipzig.", "kind": "other",
            "at_seconds": 0}])
scenario("note_repeating_event", note_lines("Yoga every Tuesday at 7 pm."),
         items=[{"text": "Yoga class", "kind": "event", "due": "every Tuesday",
                 "time": "7 pm", "owner": "Me", "at_seconds": 0}])

# --- intentions, decisions, facts in voice notes
scenario("note_intentions", note_lines(
    "I should stop prioritizing side projects.", "Figure out how it could use less battery.",
    "I should email Bob about the invoice.", "I want to start running more.",
    "Bob should focus on the bugs first.", "Think about how the relay could reconnect "
    "faster."),
    items=[{"text": "Stop prioritizing side projects", "kind": "task", "owner": "Me",
            "at_seconds": 0},
           {"text": "Figure out how it could use less battery", "kind": "task",
            "owner": "Me", "at_seconds": 5},
           {"text": "Email Bob about the invoice", "kind": "task", "owner": "Me",
            "at_seconds": 10},
           {"text": "Start running more", "kind": "task", "owner": "Me", "at_seconds": 15},
           {"text": "Focus on the bugs first", "kind": "task", "owner": "Bob",
            "at_seconds": 20},
           {"text": "Think about how the relay could reconnect faster", "kind": "task",
            "owner": "Me", "at_seconds": 25}],
    facts=[{"subject": "Me", "fact": "The owner decided to stop prioritizing side "
            "projects.", "kind": "decision", "at_seconds": 0}])
scenario("note_intention_after_list_header", note_lines(
    "Make a to-do list.", "Focus on the bugs first.", "Figure out how to cut costs."),
    items=[{"text": "Focus on the bugs first", "kind": "task", "owner": "Me",
            "at_seconds": 5}])
scenario("note_decisions_kept_uncapped", note_lines(
    "We decided to move the launch to spring.", "Bo is my brother.", "Ana likes jazz.",
    "Tom lives in Graz.", "Eva plays chess."),
    facts=[{"subject": "Me", "fact": "The owner decided to move the launch to spring.",
            "kind": "decision", "at_seconds": 0},
           {"subject": "owner", "fact": "The owner decided to sell the van.",
            "kind": "decision", "at_seconds": 0},
           {"subject": "Bo", "fact": "Bo is the owner's brother.", "kind": "relationship",
            "at_seconds": 5},
           {"subject": "Ana", "fact": "Ana likes jazz.", "kind": "preference",
            "at_seconds": 10},
           {"subject": "Tom", "fact": "Tom lives in Graz.", "kind": "personal",
            "at_seconds": 15},
           {"subject": "Eva", "fact": "Eva plays chess.", "kind": "personal",
            "at_seconds": 20},
           {"subject": "Bo", "fact": "Bo decided to sell his bike.", "kind": "decision",
            "at_seconds": 5}],
    scores=lambda f: 3 if "Tom" in f["fact"] else 4 + (f["number"] % 2))

# --- errands and passing facts
scenario("note_errands", note_lines(
    "I'll grab the keys real quick.", "Remind me to move the truck right now.",
    "Let me check with my wife and get back to you."),
    items=[{"text": "Grab the keys", "kind": "task", "owner": "Me", "at_seconds": 0},
           {"text": "Move the truck", "kind": "task", "owner": "Me", "at_seconds": 5},
           {"text": "Check with wife", "kind": "task", "owner": "Me", "at_seconds": 10}])
scenario("note_passing_facts", note_lines(
    "I was very focused today.", "Ana might move to Porto.", "Priya asked me to send the "
    "slides."),
    facts=[{"subject": "Me", "fact": "Me was very focused today.", "at_seconds": 0},
           {"subject": "Ana", "fact": "Ana might move to Porto.", "at_seconds": 5},
           {"subject": "Priya", "fact": "Priya asked me to send the slides.",
            "at_seconds": 10},
           {"subject": "Ana", "fact": "Ana is interested in hearing how the demo sounds.",
            "at_seconds": 5},
           {"subject": "Me", "fact": "Me wants Priya to review the draft.",
            "at_seconds": 10},
           {"subject": "Ana", "fact": "Ana has an allergy that might cause anaphylaxis.",
            "at_seconds": 5}],
    notes={"title": "Notes", "summary": "", "key_points": [], "people": ["Priya", "Ana"],
           "topics": []})

# --- duplicates, review, cap, known facts
scenario("note_duplicates", note_lines("Make a to-do to get eggs.", "Get eggs."),
         items=[{"text": "Get eggs", "kind": "task", "owner": "Me", "at_seconds": 0},
                {"text": " get eggs ", "kind": "task", "owner": "Me", "at_seconds": 5},
                {"text": "", "kind": "task", "at_seconds": 5}],
         facts=[{"subject": "Me", "fact": "Me likes eggs.", "at_seconds": 0},
                {"subject": "Me", "fact": "me likes eggs.", "at_seconds": 5}])
scenario("note_review_fails", note_lines("Ana likes jazz.", "Tom lives in Graz."),
         facts=[{"subject": "Ana", "fact": "Ana likes jazz.", "at_seconds": 0},
                {"subject": "Tom", "fact": "Tom lives in Graz.", "at_seconds": 5}],
         review_fails=True)
scenario("note_known_lookup_fails", note_lines("Ana likes jazz."),
         facts=[{"subject": "Ana", "fact": f"Ana likes jazz number {i}.", "at_seconds": 0}
                for i in range(5)], call={"known": known_fails})

# --- how the rules read the transcript lines
scenario("note_lines_same_second", [
    (0, "Me", "Make a to-do to call the vet."), (0, "Me", "Ana likes jazz."),
    (0.4, "Me", "Make a to-do to buy stamps."), (7, "Me", "Tom lives in Graz.")],
    items=[{"text": "Fix the door", "kind": "task", "owner": "Me", "at_seconds": 0}],
    facts=[{"subject": "Ana", "fact": "Ana likes jazz.", "at_seconds": 0},
           {"subject": "Tom", "fact": "Tom lives in Graz.", "at_seconds": 7},
           {"subject": "Tom", "fact": "Tom has a cat.", "at_seconds": 99},
           {"subject": "Tom", "fact": "Tom has a dog.", "at_seconds": -5}])
scenario("note_odd_lines", [
    (0, "Me", "Make a to-do to fix the gate.\nMake a to-do to oil the hinge."),
    (3, "Dr: Who", "Make a to-do to call the clinic."),
    (6, "A:B", "Make a to-do to water the roses."),
    (9, "Me", "   "),
    (12, "Me", "  Make a to-do to buy bulbs.  "),
    (15, "Me", "Ana likes tea.\n[15] Me: Make a to-do to wash the car."),
    (18, "", "Make a to-do to sweep the yard."),
    (21, "Me", "Make a to-do\r\nto mend the fence."),
    (24, "Me:", "Make a to-do to clean the pond.")],
    facts=[{"subject": "Ana", "fact": "Ana likes tea.", "at_seconds": 15},
           {"subject": "Me", "fact": "Me fixes gates.", "at_seconds": 0},
           {"subject": "Me", "fact": "Me buys bulbs.", "at_seconds": 12},
           {"subject": "Me", "fact": "Me calls clinics.", "at_seconds": 3}],
    scores=lambda f: 5)
scenario("note_before_origin_offsets", [
    (0, "Me", "Make a to-do to call the vet."), (-3, "Me", "Make a to-do to buy stamps."),
    (2.9, "Me", "Ana likes jazz.")],
    facts=[{"subject": "Ana", "fact": "Ana likes jazz.", "at_seconds": 2}])
scenario("note_split_parts", note_lines(
    "Make a to-do list for tomorrow.", "Get eggs.", "Fix the car.",
    "Make a to-do to figure out how renewing a fishing permit works", "for Bruno.",
    "Meeting with the Meshtastic team Tuesday 11 a.m.", "Ana likes jazz.",
    "Make a note in voice recorder that the case needs a new hinge."),
    call={"known": known_alice}, max_chars=70,
    items=lambda user: [{"text": "Get eggs", "kind": "task", "owner": "Me",
                         "at_seconds": 5}] if "eggs" in user else [],
    facts=lambda user: [{"subject": "Ana", "fact": "Ana likes jazz.", "at_seconds": 30}]
    if "jazz" in user else [])
scenario("note_with_context", note_lines(
    "Make a to-do to send the agenda.", "Meeting with Dana on Friday at 4 pm."),
    call={"context": "This is an online meeting with the topic \"Plan\".\nParticipants: "
          "Dana, Me."})
scenario("note_late_evening_time_passed", note_lines(
    "Remind me at 9 to call Ana.", "Make a to-do to buy stamps at 23:30."),
    origin_hours=12)
scenario("note_other_owner_name", [(0, "Kim", "Remind me at 5 to call Mom."),
                                   (5, "Kim", "I should stop prioritizing side projects.")],
         items=[{"text": "Stop prioritizing side projects", "kind": "task", "owner": "Kim",
                 "at_seconds": 5}],
         facts=[{"subject": "Kim", "fact": "Kim decided to stop side projects.",
                 "kind": "decision", "at_seconds": 5}],
         owner="Kim")

# --- conversations
CONV = [(0, "Alice", "I'm vegetarian, and I'll send Sam the slides on Friday."),
        (30, "Me", "Meeting with Jason on Wednesday at 12 pm on Main Street."),
        (60, "Me", "Add milk to my shopping list."),
        (90, "Me", "I'll move my truck right now."),
        (120, "Bob", "Let me check with my wife and get back to you."),
        (150, "Alice", "I'm thinking about selling the boat. I work at the Porto bakery.")]
scenario("conv_rules", CONV, kind=None, call={"known": known_alice},
         items=[{"text": "Send Sam the slides", "kind": "task", "owner": "Alice",
                 "due": "Friday", "at_seconds": 0},
                {"text": "Meet Jason", "kind": "event", "owner": "Me", "due": "Wednesday",
                 "time": "12 pm", "duration_minutes": 2000, "location": " Main Street ",
                 "at_seconds": 30},
                {"text": "Add milk to my shopping list", "kind": "task", "at_seconds": 60},
                {"text": "Walk truck to the street", "kind": "task", "owner": "Me",
                 "at_seconds": 90},
                {"text": "Check with wife", "kind": "task", "owner": "Bob",
                 "at_seconds": 120},
                {"text": "Save the wifi password", "kind": "note", "at_seconds": 150},
                {"text": "Call the plumber at 8", "kind": "task", "owner": "Me",
                 "at_seconds": 160},
                {"text": "Water the plants daily", "kind": "event", "at_seconds": 200}],
         facts=[{"subject": "Alice", "fact": "Alice is vegetarian.", "kind": "personal",
                 "at_seconds": 0},
                {"subject": "Alice", "fact": "Alice is thinking about selling the boat.",
                 "at_seconds": 150},
                {"subject": "Alice", "fact": "Alice works at the Porto bakery.",
                 "kind": "personal", "at_seconds": 150},
                {"subject": "Me", "fact": "Me has a meeting with Jason on Wednesday at "
                 "12 pm.", "at_seconds": 30},
                {"subject": "Me", "fact": "Me prefers green tea in the morning.",
                 "kind": "preference", "at_seconds": 30},
                {"subject": "Bob", "fact": "Bob is married.", "at_seconds": 120},
                {"subject": "Bob", "fact": "Bob's wife is a nurse.", "at_seconds": 120},
                {"subject": "Alice", "fact": "Alice has two cats.", "at_seconds": 0}],
         notes={"title": "Plans", "summary": "Plans.", "key_points": [" a ", ""],
                "people": ["Alice", "Me", "Bob", "Speaker 2", "unknown"],
                "topics": ["x", "X", " "]},
         scores=lambda f: [5, 4, 2, 5, 3, 4, 5, 4][f["number"] % 8])
scenario("conv_owner_silent", [(0, "Alice", "We should repaint the fence next week."),
                               (10, "Bob", "I'll buy the paint on Saturday.")],
         kind=None, items=[{"text": "Buy the paint", "kind": "task", "owner": "Bob",
                            "due": "Saturday", "at_seconds": 10}],
         notes={"title": "Fence", "summary": "Fence.", "key_points": [],
                "people": ["Alice", "Bob", "me", "Me"], "topics": []})
scenario("conv_too_short", [(0, "Me", "Hi there.")], kind=None)
scenario("conv_mic_check", [(0, "Me", "Testing, testing, one two three.")], kind=None)
scenario("conv_discarded", CONV, kind=None, discard="tv_or_media")
scenario("conv_discard_flag_reason_none", CONV, kind=None, discard="none")
scenario("conv_meeting_with_chat", [
    (0, "Dana", "Let's review the budget, I'll send the sheet tomorrow."),
    (5, "Dana", "see budget-v3.xlsx", "chat"),
    (9, "Me", "Remind me at 5 to call the vendor.")], kind=None,
    call={"context": "This is an online meeting with the topic \"Budget\".\n"
          "Participants: Dana, Me, Lee.\nLines marked \"(chat)\" were typed in the "
          "meeting chat, not spoken; keep links and file names from them exactly as "
          "written."},
    items=[{"text": "Send the budget sheet", "kind": "task", "owner": "Dana",
            "due": "tomorrow", "at_seconds": 0},
           {"text": "Call the vendor", "kind": "reminder", "owner": "Me", "time": "5 pm",
            "at_seconds": 9}],
    triage_kind="meeting")
scenario("conv_split_parts", CONV, kind=None, max_chars=90,
         items=lambda user: [{"text": "Move my truck", "kind": "task", "owner": "Me",
                              "at_seconds": 90}] if "truck" in user else [
             {"text": "Send Sam the slides", "kind": "task", "owner": "Alice",
              "at_seconds": 0}],
         facts=lambda user: [{"subject": "Alice", "fact": "Alice is vegetarian.",
                              "at_seconds": 0}])
scenario("conv_no_cap", CONV, kind=None, max_facts=0, fact_min_score=1,
         facts=[{"subject": "X", "fact": f"X likes thing {i}.", "at_seconds": 0}
                for i in range(7)])
scenario("conv_hedged_and_events", [
    (0, "Me", "Can you remind me to renew my passport?"),
    (10, "Ana", "Meeting with the Lisbon office on Thursday at 3 pm."),
    (20, "Me", "I need to do the dishes."),
    (30, "Ana", "I wonder if the car needs new tyres soon.")], kind=None,
    items=[{"text": "Renew my passport", "kind": "task", "owner": "Me", "at_seconds": 0},
           {"text": "Meet the Lisbon office", "kind": "event", "owner": "Ana",
            "due": "Thursday", "time": "3 pm", "at_seconds": 10},
           {"text": "Do the dishes", "kind": "task", "owner": "Me", "at_seconds": 20},
           {"text": "Check the car tyres", "kind": "task", "owner": "Ana",
            "at_seconds": 30}],
    facts=[{"subject": "Ana", "fact": "Ana meets the Lisbon office on Thursday.",
            "at_seconds": 10},
           {"subject": "Ana", "fact": "Ana has a meeting with the Lisbon office.",
            "at_seconds": 10}])

# --- a corpus of one-sentence voice notes: the rules alone (no model items)
CORPUS = [
    "Add a task to send the slides to Greta by Friday.",
    "Add coffee filters to shopping list.",
    "Add eggs to my shopping list and make a todo to call the vet.",
    "Add eggs to my shopping list.",
    "Add pick up the parcel to my to-do list.",
    "Add salt and pepper to my shopping list.",
    "Add soda to Shopping List.",
    "Add this to my hardware notes. The screen is 1.4 inch.",
    "Add to my car note: the left tyre is low.",
    "Add to my hardware notes. The screen is 1.4 inch.",
    "Add to my notes: the gate code is 4512.",
    "Add to my shopping list eggs.",
    "Add to my to-do list: buy stamps.",
    "Add to the shopping list: oat milk.",
    "Alright, add a to-do to pick up the dry cleaning.",
    "Bob should focus on the bugs first.",
    "Call the plumber about the tap.",
    "Can you make a to-do to call the bank?",
    "Can you remind me to renew my passport?",
    "Create a note. Battery dimensions are 13 mm by 14 mm by 3 mm.",
    "Create a to-do called replace the smoke alarm battery.",
    "Focus on testing existing bugs before adding features.",
    "From now on I'll test every pull request before merging it.",
    "Hey, um, so, make a to-do to get eggs.",
    "Hold on, I'll get the door in a second.",
    "I need to do the dishes.",
    "I need to sleep more.",
    "I should be more patient with the team.",
    "I should stop prioritizing side projects from Monday.",
    "I want to make a to-do to fix the bike brakes.",
    "I want to make it to do the garden in order.",
    "I'll send you the slides tonight.",
    "I'm going to move my car right now.",
    "Let me add milk to my shopping list.",
    "Let me call the plumber on Friday.",
    "Let's make a to-do to clean out the garage.",
    "Make a list of what to pack for the trip.",
    "Make a note for Sam that the key is under the mat.",
    "Make a note for later: the gate code is 4512.",
    "Make a note for the voice recorder that the case needs a hole for the USB port.",
    "Make a note in case I forget, the gate code is 4512.",
    "Make a note in my notes that the gate code is 4512.",
    "Make a note in the morning that the shop opens at 9.",
    "Make a note in voice recorder: case needs to be 3 mm bigger.",
    "Make a note on Friday that the shop is closed.",
    "Make a note to bring the slides.",
    "Make a note to call mom, she was sick.",
    "Make a note under car, the oil change is due at 90000 km.",
    "Make a shopping list. Eggs. Milk. Bread.",
    "Make a to-do list for tomorrow: call the vet, get eggs.",
    "Make a to-do list. Get eggs. Fix the car. Call the vet.",
    "Make a todo to get eggs and make a todo to get milk.",
    "New note: the left speaker crackles at high volume.",
    "New task, back up the laptop.",
    "New to-do: order a replacement charger.",
    "Note to self, the spare key is under the blue pot.",
    "Put batteries on the hardware store list.",
    "Put call the landlord on my to do list.",
    "Remember to buy milk. And create a note the garage code is 4417.",
    "Remind me Friday to send the slides.",
    "Remind me to take the bins out at 7 pm.",
    "Remind me tomorrow to take out the recycling.",
    "Think about how the relay could reconnect faster.",
    "To do: renew the car insurance.",
    "Todo: stop prioritizing side projects.",
    "Um, add a hammer to my hardware list.",
    "Um, make a note that the boiler pressure is low.",
    "We decided to move the launch to spring.",
    "Book a table for Saturday at 8 pm at the Thai place.",
    "Dinner with Maria and Paul next Friday at 7:30 pm.",
    "Doctor's appointment on the 14th at 9 a.m.",
    "Call with the bank tomorrow morning at 10.",
    "Make a note that I met Jo at the market. Make a to-do to email Jo the photos.",
    "Make a to-do to ask Lee about the invoice, then remind me at 6 to cook.",
    "Okay so make a to-do to... um... sort the garage.",
    "I'd like to add a task to email Ana.",
    "Make a grocery list: apples, pears, and plums.",
    "Write down on my packing list: charger, passport, socks.",
    "Interview with Acme on Monday at 2 pm, make a note to print the CV.",
]


def corpus_run(sent: list[str]) -> dict[str, Any]:
    out = {}
    for said in CORPUS:
        s = summarizer(sent, min_words=1)
        result = s.summarize(segs(note_lines(said)), kind="note")
        out[said] = {"action_items": result["action_items"], "facts": result["facts"]}
    return out


def run_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (call, fake, lines) in SCENARIOS.items():
        sent: list[str] = []
        fake = dict(fake)
        opts = {k: fake.pop(k) for k in ("owner", "tz", "max_chars", "min_words",
                                         "max_facts", "fact_min_score") if k in fake}
        origin = ORIGIN + 3600 * fake.pop("origin_hours", 0)
        if "triage_kind" in fake:
            fake["kind"] = fake.pop("triage_kind")
        result = summarizer(sent, **opts, **fake).summarize(segs(lines, origin), **call)
        out[name] = {"requests": sent, "result": result}

    sent = []
    out["corpus_voice_notes"] = {"requests": [], "result": corpus_run(sent)}
    out["corpus_voice_notes"]["requests"] = sent

    new = [{"subject": "Alice", "fact": "Alice is vegetarian."},
           {"subject": "Alice", "fact": "Alice works at the Lisbon bakery now."},
           {"subject": "Me", "fact": "Me likes tea."},
           {"subject": "Me", "fact": "Me will send the spreadsheet."}]
    known = [{"id": 7, "learned": "2025-09-01", "subject": "Alice",
              "fact": "Alice is a vegetarian cook."},
             {"id": 8, "subject": "Alice", "fact": "Alice works at the Porto bakery."},
             {"id": 9, "learned": "2025-09-03", "subject": "Me",
              "fact": "Me avoids hard food."}]
    links = [{"new": 0, "relation": "same", "existing": 7},
             {"new": 1, "relation": "updates", "existing": 8},
             {"new": 2, "relation": "new"},
             {"new": 3, "relation": "same", "existing": 9},
             {"new": 4, "relation": "same", "existing": 7},
             {"new": -1, "relation": "new"},
             {"new": 2, "relation": "updates", "existing": 99},
             {"new": 1, "relation": "same"}]
    for name, args in {"link_facts_mixed": (new, known),
                       "link_facts_no_new": ([], known),
                       "link_facts_no_known": (new, [])}.items():
        sent = []
        result = [lk.model_dump() for lk in summarizer(sent, links=links).link_facts(
            "2025-10-06", *args)]
        out[name] = {"requests": sent, "result": result}

    forget = {"title": "Alice is vegetarian at lunch", "summary": "Plans were made. "
              "Alice, is vegetarian! A trip was planned.  Bob is vegetarian too?",
              "key_points": ["alice is VEGETARIAN", "Trip", "Bob is vegetarian too"],
              "topics": ["food", "Alice is vegetarian"]}
    notes = {"title": "Plans", "summary": "Plans were made. Alice is vegetarian.",
             "key_points": ["Alice is vegetarian", "Trip"], "topics": ["food"]}
    for name, args in {
            "remove_facts_scrubbed": (notes, ["Alice is vegetarian", "  ", "Bob is "
                                              "vegetarian too"]),
            "remove_facts_none": (notes, []),
            "remove_facts_empty_notes": ({"title": None, "summary": "", "key_points": None},
                                         ["Alice is vegetarian"])}.items():
        sent = []
        out[name] = {"requests": sent, "result": summarizer(sent, forget=forget)
                     .remove_facts(*args)}

    sent = []
    out["digest_custom_skip_lists"] = {"requests": sent, "result": summarizer(
        sent, digest_skip_lists=" Work , ,the hardware store list").digest("2025-10-06", [
            {"id": "c1", "started_at": ORIGIN, "title": "T", "summary": "S",
             "key_points": [], "people": [],
             "action_items": [{"text": "A", "list_name": "work"},
                              {"text": "B", "list_name": "hardware store"},
                              {"text": "C", "list_name": "Shopping"},
                              {"text": "D", "status": "dismissed"}]}])}
    return out


def test_summarizer_rules_are_unchanged() -> None:
    got = json.dumps(snapshot(run_all()), indent=1, ensure_ascii=False,
                     sort_keys=True) + "\n"
    if UPDATE:
        SNAPSHOT.write_text(got, encoding="utf-8")
    want = SNAPSHOT.read_text(encoding="utf-8")
    assert got == want, ("summarizer rules changed their results; if that is meant, "
                         "UPDATE_SNAPSHOTS=1 pytest "
                         "tests/unit/summarize/test_summarize_rules_golden.py")
