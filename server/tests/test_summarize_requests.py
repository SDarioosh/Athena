"""The exact requests the summarizer sends to the LLM, and what it makes of the answers.

Prompts and JSON schemas drive the answers: one changed character can change eval scores.
This test sends a fixed set of fictional recordings through ``Summarizer`` with a fake
llama-server and compares, byte for byte, every request body (messages and
``response_format``) and every result with ``tests/fixtures/summarize_requests.json``.

A change to a prompt, a schema or a rule that is meant to change them rewrites the file:

    UPDATE_SNAPSHOTS=1 pytest tests/test_summarize_requests.py
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

from athena.llm import LLMClient, Summarizer
from athena.store import Segment

SNAPSHOT = Path(__file__).parent / "fixtures" / "summarize_requests.json"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

# Monday 2025-10-06 09:00 UTC
ORIGIN = 1759741200.0

CONVERSATION_ITEMS = [
    {"text": "Send Sam the slides", "title": "Send Sam the slides", "kind": "task",
     "owner": "Me", "due": "Friday", "due_date": "2025-10-09", "at_seconds": 0},
    {"text": "Meet Jason", "kind": "event", "owner": "Me", "due": "Wednesday",
     "time": "12 pm", "duration_minutes": 60, "location": "Main Street", "at_seconds": 30},
    {"text": "Add milk to my shopping list", "kind": "task", "owner": "Me",
     "at_seconds": 60},
    {"text": "Move my truck", "kind": "task", "owner": "Me", "at_seconds": 90},
    {"text": "Water the plants every Monday", "kind": "event", "owner": "Me",
     "at_seconds": 120},
    {"text": "Save the wifi password", "kind": "note", "at_seconds": 150},
]
CONVERSATION_FACTS = [
    {"subject": "Alice", "fact": "Alice is vegetarian.", "kind": "personal", "at_seconds": 0},
    {"subject": "Me", "fact": "Me was very focused today.", "kind": "other", "at_seconds": 30},
    {"subject": "Me", "fact": "Me is considering selling the boat.", "kind": "plan",
     "at_seconds": 60},
    {"subject": "Jason", "fact": "Jason has a meeting with Me on Wednesday at 12 pm.",
     "kind": "plan", "at_seconds": 30},
    {"subject": "Me", "fact": "Me wants Alice to send the report.", "kind": "other",
     "at_seconds": 90},
    {"subject": "Alice", "fact": "Alice moved to Lisbon in May.", "kind": "odd-kind",
     "at_seconds": 120},
    {"subject": "Alice", "fact": "Alice works at the Porto bakery.", "kind": "personal",
     "at_seconds": 150},
    {"subject": "Me", "fact": "Me plans a trip to Lisbon next spring.", "kind": "plan",
     "at_seconds": 180},
]
CONVERSATION_LINES = [
    (0, "Alice", "I'm vegetarian, so no fish for me. I'll send Sam the slides on Friday."),
    (30, "Me", "Meeting with Jason on Wednesday at 12 pm on Main Street, for an hour."),
    (60, "Me", "Add milk to my shopping list."),
    (90, "Me", "I'll move my truck right now."),
    (120, "Alice", "I moved to Lisbon in May and I water the plants every Monday."),
    (150, "Alice", "I work at the Porto bakery."),
    (180, "Me", "We plan a trip to Lisbon next spring."),
]

NOTE_ITEMS = [
    {"text": "Get eggs", "kind": "task", "owner": "Me", "at_seconds": 0},
    {"text": "Battery dimensions are 13 mm x 14 mm x 3 mm", "kind": "note",
     "title": "Battery dimensions", "at_seconds": 10},
    {"text": "Stop prioritizing side projects", "kind": "task", "owner": "Me",
     "at_seconds": 20},
    {"text": "Figure out how it could use less battery", "kind": "task", "owner": "Me",
     "at_seconds": 30},
    {"text": "Eggs, milk and bread", "kind": "task", "list_name": "shopping",
     "owner": "Me", "at_seconds": 40},
]
NOTE_FACTS = [
    {"subject": "Me", "fact": "The owner decided to stabilise the base system first.",
     "kind": "decision", "at_seconds": 20},
    {"subject": "Bo", "fact": "Bo is the owner's brother.", "kind": "relationship",
     "at_seconds": 50},
]
NOTE_LINES = [
    (0, "Me", "Um, make a to-do to get eggs tomorrow."),
    (10, "Me", "Create a note battery dimensions are 13mm x 14mm x 3mm."),
    (20, "Me", "I should stop prioritizing side projects before the base system is stable."),
    (30, "Me", "Figure out how it could use less battery."),
    (40, "Me", "Add eggs, milk and bread to my shopping list. Remind me at 5 to call Mom."),
    (50, "Me", "Meeting with the design team Tuesday 11 a.m. Bo is my brother."),
    (60, "Me", "Add to my hardware notes: the screen is 1.4 inch."),
]


def segments(lines: list[tuple[int, str, str]]) -> list[Segment]:
    return [Segment(ORIGIN + at, ORIGIN + at + 9, text, speaker, is_owner=speaker == "Me")
            for at, speaker, text in lines]


def transport(sent: list[str], items: list[dict[str, Any]], facts: list[dict[str, Any]],
              kind: str = "casual", discard: bool = False) -> httpx.MockTransport:
    """Fake llama-server: records each request body as sent, answers by schema name."""
    notes = {"title": "Plans with Alice", "summary": "Alice and the user made plans. "
             "Alice is vegetarian.", "key_points": ["Alice is vegetarian", "Trip in spring"],
             "people": ["Alice", "Me", "Speaker 1", "alice"], "topics": ["travel", "Travel"]}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        user = body["messages"][-1]["content"]
        if name == "FactReview":
            asked = json.loads(user)["facts"]
            content: Any = {"scores": [{"number": f["number"], "score": 5 - f["number"] % 3}
                                       for f in asked]}
        else:
            content = {
                "Triage": {"discard": discard, "reason": "tv_or_media" if discard else "none",
                           "kind": kind},
                "SummaryNotes": notes,
                "ActionItemList": {"action_items": items},
                "FactList": {"facts": facts},
                "FactLinks": {"links": [{"new": 0, "relation": "same", "existing": 7},
                                        {"new": 1, "relation": "updates", "existing": 8},
                                        {"new": 2, "relation": "new"}]},
                "DailyDigest": {"headline": "A day of plans", "summary": "Plans.",
                                "highlights": ["Lisbon"], "open_action_items": ["Slides"],
                                "people": ["Alice"]},
                "ForgetRewrite": {"title": "Alice is vegetarian", "summary": "Plans were "
                                  "made. Alice is vegetarian. A trip was planned.",
                                  "key_points": ["Alice is vegetarian", "Trip"],
                                  "topics": ["food"]},
            }[name]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(content)}}]})

    return httpx.MockTransport(handler)


def summarizer(sent: list[str], items: list[dict[str, Any]], facts: list[dict[str, Any]],
               max_chars: int = 120_000, **kw: Any) -> Summarizer:
    llm = LLMClient("http://llm.test/v1", "test-model",
                    transport=transport(sent, items, facts, **kw))
    return Summarizer(llm, "Me", max_chars_per_call=max_chars, timezone="Europe/Lisbon")


def known(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"subject": "Alice", "fact": "Alice works at a bakery in Porto."}]


def run_all() -> dict[str, Any]:
    out: dict[str, Any] = {}

    def record(name: str, fn: Any, **kw: Any) -> None:
        sent: list[str] = []
        result = fn(sent, **kw)
        out[name] = {"requests": sent, "result": result}

    for kind in ("meeting", "call", "appointment", "casual", "other"):
        record(f"conversation_{kind}", lambda sent, kind=kind: summarizer(
            sent, CONVERSATION_ITEMS, CONVERSATION_FACTS, kind=kind).summarize(
            segments(CONVERSATION_LINES), known=known))
    record("conversation_split_with_context", lambda sent: summarizer(
        sent, CONVERSATION_ITEMS, CONVERSATION_FACTS, max_chars=150).summarize(
        segments(CONVERSATION_LINES), context="Online meeting: Planning\nParticipants: "
        "Alice, Me"))
    record("conversation_discarded", lambda sent: summarizer(
        sent, CONVERSATION_ITEMS, CONVERSATION_FACTS, discard=True).summarize(
        segments(CONVERSATION_LINES)))
    record("conversation_filler", lambda sent: summarizer(
        sent, CONVERSATION_ITEMS, CONVERSATION_FACTS).summarize(
        segments([(0, "Me", "Um, okay, yeah.")])))
    record("voice_note", lambda sent: summarizer(sent, NOTE_ITEMS, NOTE_FACTS).summarize(
        segments(NOTE_LINES), kind="note", known=known))
    record("voice_note_split", lambda sent: summarizer(
        sent, NOTE_ITEMS, NOTE_FACTS, max_chars=120).summarize(
        segments(NOTE_LINES), kind="note"))
    record("link_facts", lambda sent: [lk.model_dump() for lk in summarizer(
        sent, [], []).link_facts("2025-10-06", [
            {"subject": "Alice", "fact": "Alice is vegetarian."},
            {"subject": "Alice", "fact": "Alice works at the Lisbon bakery now."},
            {"subject": "Me", "fact": "Me likes tea."}],
        [{"id": 7, "learned": "2025-09-01", "subject": "Alice",
          "fact": "Alice is a vegetarian cook."},
         {"id": 8, "learned": "2025-09-02", "subject": "Alice",
          "fact": "Alice works at the Porto bakery."}])])
    record("digest", lambda sent: summarizer(sent, [], []).digest("2025-10-06", [
        {"id": "c1", "started_at": ORIGIN, "title": "Plans", "summary": "Plans were made.",
         "key_points": ["Trip"], "people": ["Alice"],
         "action_items": [{"text": "Send slides"}, {"text": "Milk", "list_name": "shopping"},
                          {"text": "A note", "kind": "note"},
                          {"text": "Done", "status": "done"}]},
        {"id": "c2", "started_at": ORIGIN + 3600, "title": "Idea", "summary": "An idea.",
         "key_points": [], "people": [], "kind": "note"},
        {"id": "c3", "started_at": ORIGIN + 7200, "title": "Written", "summary": "Written.",
         "key_points": [], "people": [], "kind": "note", "written": True}]))
    record("remove_facts", lambda sent: summarizer(sent, [], []).remove_facts(
        {"title": "Plans", "summary": "Plans were made. Alice is vegetarian.",
         "key_points": ["Alice is vegetarian", "Trip"], "topics": ["food"]},
        ["Alice is vegetarian"]))
    return out


def snapshot(runs: dict[str, Any]) -> dict[str, Any]:
    """Each request as the sha256 of its exact bytes, its schema name and user message;
    the system prompts and schemas once each, so a change shows as a readable diff."""
    prompts: dict[str, str] = {}
    schemas: dict[str, Any] = {}
    scenarios: dict[str, Any] = {}
    for name, run in runs.items():
        requests = []
        for raw in run["requests"]:
            body = json.loads(raw)
            fmt = body["response_format"]
            schema = fmt["json_schema"]["name"]
            schemas[schema] = fmt
            system = body["messages"][0]["content"]
            prompt = hashlib.sha256(system.encode()).hexdigest()[:12]
            prompts[prompt] = system
            rest = {k: v for k, v in body.items() if k not in ("messages", "response_format")}
            requests.append({"sha256": hashlib.sha256(raw.encode()).hexdigest(),
                             "schema": schema, "system": prompt,
                             "user": body["messages"][1]["content"], "rest": rest})
        scenarios[name] = {"requests": requests, "result": run["result"]}
    return {"prompts": prompts, "schemas": schemas, "scenarios": scenarios}


def test_summarizer_requests_and_results_are_unchanged() -> None:
    got = json.dumps(snapshot(run_all()), indent=1, ensure_ascii=False,
                     sort_keys=True) + "\n"
    if UPDATE:
        SNAPSHOT.write_text(got, encoding="utf-8")
    want = SNAPSHOT.read_text(encoding="utf-8")
    assert got == want, ("summarizer requests or results changed; if that is meant, "
                         "UPDATE_SNAPSHOTS=1 pytest tests/test_summarize_requests.py")
