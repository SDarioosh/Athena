# ruff: noqa: E501  (the demo transcript lines, as in scripts/seed_demo.py)
"""A fixed, fictional store for the contract snapshots.

The people and events come from ``scripts/seed_demo.py`` (all made up), plus a few
recordings that cover what the demo lacks (an unnamed speaker, a written note, a spoken
reminder). Summaries come from a canned fake of llama-server keyed by each recording's
first line, so the real pipeline (triage, notes, action items, facts, fact review, fact
links, digests) runs without a model. The clock and conversation ids are fixed while the
store is built, so every build writes the same bytes into the same rows.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest

from athena.config import Settings
from athena.db import conversations as store_module
from athena.db.store import Store
from athena.domain import CONVERSATION, NOTE, Segment
from athena.pipeline import Pipeline
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from athena.vad import EnergyVAD

ME = "Me"
INGEST_TOKEN = "contract-ingest-token-0123456789"  # signs the transcript links
MCP_TOKEN = "contract-mcp-token-0123456789ab"
PUBLIC_URL = "https://athena.example.org"
TIMEZONE = "Europe/Berlin"
# The fixed clock: 2026-10-20 12:00:00 UTC, after every recording below.
NOW = 1_792_497_600.0


def settings_for(data_dir: Path) -> Settings:
    """Settings for the contract store; nothing is read from the environment or .env."""
    return Settings(_env_file=None, ingest_token=INGEST_TOKEN, data_dir=data_dir,
                    run_worker=False, vad_backend="energy", timezone=TIMEZONE,
                    public_url=PUBLIC_URL, mcp_token=MCP_TOKEN, owner_name=ME,
                    llm_base_url="http://llm.test/v1", llm_model="demo-model")


def frozen(mp: pytest.MonkeyPatch) -> None:
    """Fix the clock and the conversation ids (uuid4 in the store) on ``mp``."""
    mp.setattr(time, "time", lambda: NOW)
    counter = iter(range(1, 10_000))
    mp.setattr(store_module, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=f"{0xC0DE0000 + next(counter):032x}")))


def voice(n: int) -> np.ndarray:
    """A unit 'voice' embedding, one direction per person."""
    v = np.full(16, 0.01, dtype=np.float32)
    v[n] = 1.0
    return v / np.linalg.norm(v)


# ------------------------------------------------------------------ recordings
# (key, start UTC, device, [(speaker, text), ...]); lines are 8 s apart. Keys starting
# with "note-" are voice notes. From seed_demo.py, plus "bakery" and "note-call-mom".
CONVERSATIONS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("budget-sep", "2026-09-15T10:00", "trex3pro", [
        ("Alice", "Morning. So finance came back on the Q4 budget and it's not great."),
        (ME, "How bad is it?"),
        ("Alice", "They're cutting our budget by fifteen percent. Marketing takes most of the hit."),
        (ME, "Okay. I'll rework the spreadsheet with the new numbers."),
        ("Alice", "Great, can you send me the revised spreadsheet by Friday?"),
        (ME, "Yes, I'll send it by Friday."),
        ("Alice", "Also, between us, I'm moving to the Berlin office in January."),
        (ME, "Wow, congratulations! We'll miss you here."),
    ]),
    ("maria-sep", "2026-09-22T19:30", "trex3pro", [
        ("Maria", "Hey! Did you remember Mom turns seventy on November eighth?"),
        (ME, "Of course. We should do a dinner."),
        ("Maria", "I'll book the restaurant, I'm thinking Casa Lupo, she loves it."),
        (ME, "Perfect. I'll take care of the gift, maybe a photo album with old family pictures."),
        ("Maria", "She'll cry, in a good way. Oh, and I start my new job at the hospital in October."),
        (ME, "That's great news, congratulations."),
    ]),
    ("dentist", "2026-09-30T14:00", "omi", [
        ("Dr. Patel", "The X-ray shows the lower left molar is cracked. You'll need a crown."),
        (ME, "Is it urgent?"),
        ("Dr. Patel", "Not an emergency, but let's not wait. Your follow-up is October twenty-first at nine a.m."),
        ("Dr. Patel", "Until then, avoid hard food on that side, no nuts or hard candy."),
        (ME, "Got it, October twenty-first at nine."),
    ]),
    ("landlord", "2026-10-03T11:00", "trex3pro", [
        ("Tom", "About the lease renewal: the rent goes from fourteen fifty to fifteen twenty euros starting January."),
        (ME, "That's a big jump. Is that final?"),
        ("Tom", "It is, costs went up for me too. I need the signed renewal by October thirty-first."),
        (ME, "Okay. And the bathroom fan is still broken."),
        ("Tom", "I'll come fix the bathroom fan next week, promise."),
    ]),
    ("standup", "2026-10-07T09:30", "trex3pro", [
        ("Ben", "I finished the API migration yesterday, everything is on the new endpoints."),
        ("Alice", "Nice work. Heads up, the client demo moved to October twenty-third."),
        ("Ben", "I'm a bit worried about database load during the demo, the new queries are heavy."),
        (ME, "Let's look at it together. Ben, I'll review your pull request today."),
        ("Ben", "Thanks, that would help."),
    ]),
    ("jonas-dinner", "2026-10-10T18:00", "omi", [
        ("Jonas", "I signed up for the Lisbon marathon in December, training every morning now."),
        (ME, "That's ambitious! How far are you running?"),
        ("Jonas", "Thirty kilometers on Sundays. Also you have to read The Overstory, best book I read this year."),
        (ME, "Adding it to my list. Do you want the satay?"),
        ("Jonas", "No thanks, I'm allergic to peanuts, remember?"),
    ]),
    ("note-bike", "2026-10-11T08:15", "twatch", [
        (ME, "Note to self: the bike needs new brake pads before winter."),
        (ME, "Ask at the bike shop on Lindenstrasse, and check if the rear light still works."),
    ]),
    ("alice-1on1", "2026-10-14T15:00", "trex3pro", [
        ("Alice", "It's official, my Berlin move date is January twelfth."),
        (ME, "So soon. Who takes over your vendor contracts?"),
        ("Alice", "I'd like you to take over the vendor contracts. Can you?"),
        (ME, "Yes, I'll take over the vendor contracts."),
        ("Alice", "Thank you. And good news on the budget: the cut is now ten percent, not fifteen."),
    ]),
    ("note-offsite", "2026-10-15T07:40", "twatch", [
        (ME, "Idea for the team offsite: a pasta-making cooking class instead of the escape room."),
        (ME, "There's a cooking school near the river. Ask Alice before she leaves for Berlin."),
    ]),
    ("maria-oct", "2026-10-16T20:00", "omi", [
        ("Maria", "Casa Lupo is booked for seven p.m. on November eighth, twelve guests."),
        (ME, "Great. Who's coming from out of town?"),
        ("Maria", "Aunt Rosa takes the train. Can you pick her up from the train station that afternoon?"),
        (ME, "Sure, I'll pick up Aunt Rosa from the station."),
    ]),
    # A TV left on: discarded, not summarized.
    ("tv-news", "2026-10-17T21:00", "omi", [
        ("Speaker 1", "Good evening, and welcome to the nine o'clock news."),
        ("Speaker 1", "Tonight's top story: heavy rain is expected across the northern region over the weekend."),
        ("Speaker 2", "In sport, the home side won three to one after a late goal in the final minutes."),
        ("Speaker 1", "Stay with us after the break for the full weather forecast."),
    ]),
    # Nobody has named the baker yet: an unnamed speaker with a stored voice.
    ("bakery", "2026-10-18T09:00", "omi", [
        ("Speaker 1", "Morning! Your sourdough is ready, and the rye bread comes out of the oven at four."),
        (ME, "Great, I'll come back at four for the rye."),
        ("Speaker 1", "I'll keep one aside for you. We're closed on Monday for the holiday."),
        (ME, "Good to know, thanks."),
    ]),
    ("note-call-mom", "2026-10-19T07:30", "twatch", [
        (ME, "Remind me tomorrow at 6 p.m. to call Mom about the birthday dinner."),
    ]),
]

# Zoom meetings (device "zoom"): (key, start UTC, [(speaker, text, at seconds, length)]).
MEETINGS: list[tuple[str, str, list[tuple[str, str, float, float]]]] = [
    ("zoom-newsletter", "2026-10-09T16:00", [
        ("Priya Example", "Thanks for joining. Today is about relaunching the community "
         "newsletter.", 0, 6),
        (ME, "Great. I looked at the numbers: only twenty percent of readers open it now.", 8, 6),
        ("Lena Example", "I think the problem is the length, nobody reads twelve sections.", 16, 6),
        ("Priya Example", "Agreed. Let's cut it down to three sections and a photo of "
         "the month.", 24, 6),
        ("Lena Example", "And we could send it on Sunday mornings instead of Tuesdays, "
         "people have more time then.", 30, 7),
        ("Omar Example", "Sorry to jump in, Sunday works for me, I can write the welcome "
         "email for new readers.", 33, 6),
        (ME, "Good. I'll set up the new template in the mailing tool.", 41, 5),
        ("Priya Example", "Then the first new issue goes out on Sunday, November "
         "fifteenth.", 48, 5),
        ("Omar Example", "I'll have the welcome email ready a week before that.", 55, 5),
    ]),
    ("zoom-budget", "2026-10-13T15:00", [
        ("Sam Example", "Let's go through the Q4 budget for the community garden project.",
         0, 6),
        (ME, "The main open item is the tool shed, the quote came in at four thousand "
         "euros.", 8, 6),
        ("Sam Example", "That's more than we planned. Dana, do you have the updated sheet?",
         16, 5),
        ("Kai Example", "The second supplier is cheaper, about three thousand two hundred.",
         30, 5),
        (ME, "Then let's go with the second supplier. I'll confirm the order by Friday.",
         38, 5),
        ("Sam Example", "Agreed. That's all for today, thanks everyone.", 46, 4),
    ]),
]

MEETING_CHAT: dict[str, tuple[str, list[str], list[tuple[str, str, float]]]] = {
    "zoom-newsletter": ("Community newsletter relaunch",
                        ["Priya Example", ME, "Lena Example", "Omar Example"], []),
    "zoom-budget": ("Q4 budget review: community garden",
                    ["Sam Example", ME, "Dana Example", "Kai Example", "Rafael Example"], [
        ("Dana Example", "Here's the updated sheet: "
         "https://files.example.org/garden-budget-v3.xlsx", 21),
        ("Dana Example", "Tab 2 has the shed quotes from both suppliers.", 24),
    ]),
}

WRITTEN_NOTE = {"source_id": "hub-note:demo-1",
                "text": "Weekend errands\nBuy oat milk and coffee beans\n"
                        "Ask Maria which cake Mom likes",
                "written_at": "2026-10-19T17:00:00+00:00",
                "link": "https://hub.example.org/notes/demo-1", "title": "Weekend errands"}

# ----------------------------------------------------------- canned summaries
# Keyed by a phrase of the recording's first line. Facts and items use ``at_seconds``.
_ALICE_MOVE = "Alice is moving to the Berlin office in January"
_MOM_70 = "Mom turns seventy on November eighth"

CANNED: dict[str, dict[str, Any]] = {
    "finance came back on the Q4 budget": {
        "kind": "meeting", "title": "Q4 budget cut",
        "summary": "Alice said finance is cutting the Q4 budget by fifteen percent; the "
                   "user will send a revised spreadsheet by Friday.",
        "key_points": ["Q4 budget cut by fifteen percent", "Alice moves to Berlin in January"],
        "people": ["Alice", ME], "topics": ["budget", "Berlin"],
        "action_items": [{"text": "Send Alice the revised budget spreadsheet", "owner": ME,
                          "due": "Friday", "at_seconds": 32}],
        "facts": [{"subject": "Alice", "fact": _ALICE_MOVE, "kind": "plan", "at_seconds": 48},
                  {"subject": "Q4 budget", "fact": "The Q4 budget cut is fifteen percent",
                   "kind": "other", "at_seconds": 16}]},
    "Mom turns seventy": {
        "kind": "casual", "title": "Mom's seventieth birthday",
        "summary": "Maria and the user planned a dinner for Mom's seventieth birthday at "
                   "Casa Lupo; the user takes care of the gift.",
        "key_points": ["Dinner at Casa Lupo", "Photo album as the gift"],
        "people": ["Maria", ME, "Mom"], "topics": ["family", "birthday"],
        "action_items": [{"text": "Make a photo album for Mom's birthday", "owner": ME,
                          "at_seconds": 24}],
        "facts": [{"subject": "Mom", "fact": _MOM_70, "kind": "personal", "at_seconds": 0},
                  {"subject": "Maria", "fact": "Maria starts a new job at the hospital in "
                   "October", "kind": "personal", "at_seconds": 32}]},
    "lower left molar is cracked": {
        "kind": "appointment", "title": "Dentist: cracked molar",
        "summary": "Dr. Patel found a cracked lower left molar that needs a crown; the "
                   "follow-up is on October 21 at 9 a.m.",
        "key_points": ["Crown needed", "Avoid hard food until then"],
        "people": ["Dr. Patel", ME], "topics": ["health"],
        "action_items": [{"text": "Dentist follow-up", "kind": "event",
                          "due": "October twenty-first at nine a.m.", "time": "9 a.m.",
                          "at_seconds": 16}],
        "facts": [{"subject": ME, "fact": "The user needs a crown on the lower left molar",
                   "kind": "personal", "at_seconds": 0}]},
    "About the lease renewal": {
        "kind": "appointment", "title": "Lease renewal and rent increase",
        "summary": "Tom raises the rent from 1450 to 1520 euros from January and needs the "
                   "signed renewal by October 31; he will fix the bathroom fan.",
        "key_points": ["Rent rises to 1520 euros", "Renewal due October 31"],
        "people": ["Tom", ME], "topics": ["housing"],
        "action_items": [{"text": "Return the signed lease renewal to Tom", "owner": ME,
                          "due": "October thirty-first", "at_seconds": 16},
                         {"text": "Fix the bathroom fan", "owner": "Tom", "due": "next week",
                          "at_seconds": 32}],
        "facts": [{"subject": "Rent", "fact": "The rent goes up to 1520 euros a month "
                   "from January", "kind": "other", "at_seconds": 0}]},
    "finished the API migration": {
        "kind": "meeting", "title": "Standup: API migration and client demo",
        "summary": "Ben finished the API migration; the client demo moved to October 23 "
                   "and the user will review Ben's pull request.",
        "key_points": ["API migration done", "Client demo on October 23"],
        "people": ["Ben", "Alice", ME], "topics": ["work"],
        "action_items": [{"text": "Review Ben's pull request", "owner": ME, "due": "today",
                          "at_seconds": 24}],
        "facts": [{"subject": "Ben", "fact": "Ben finished the API migration to the new "
                   "endpoints", "kind": "other", "at_seconds": 0}]},
    "signed up for the Lisbon marathon": {
        "kind": "casual", "title": "Dinner with Jonas",
        "summary": "Jonas is training for the Lisbon marathon and recommended The Overstory.",
        "key_points": ["Lisbon marathon in December", "Read The Overstory"],
        "people": ["Jonas", ME], "topics": ["running", "books"],
        "action_items": [{"text": "Read The Overstory", "owner": ME, "at_seconds": 16}],
        "facts": [{"subject": "Jonas", "fact": "Jonas is allergic to peanuts",
                   "kind": "personal", "at_seconds": 32},
                  {"subject": "Jonas", "fact": "Jonas runs the Lisbon marathon in December",
                   "kind": "plan", "at_seconds": 0}]},
    "the bike needs new brake pads": {
        "title": "Bike brake pads",
        "summary": "The bike needs new brake pads before winter; ask at the bike shop on "
                   "Lindenstrasse and check the rear light.",
        "key_points": ["New brake pads before winter", "Check the rear light"],
        "people": [ME], "topics": ["bike"],
        "action_items": [{"text": "Buy new brake pads for the bike", "owner": ME,
                          "due": "before winter", "at_seconds": 0}],
        "facts": []},
    "my Berlin move date is January twelfth": {
        "kind": "meeting", "title": "1:1 with Alice: vendor contracts",
        "summary": "Alice moves to Berlin on January 12; the user takes over her vendor "
                   "contracts and the budget cut is now ten percent.",
        "key_points": ["Alice's move date is January 12", "Budget cut now ten percent"],
        "people": ["Alice", ME], "topics": ["work", "budget"],
        "action_items": [{"text": "Take over Alice's vendor contracts", "owner": ME,
                          "at_seconds": 24}],
        "facts": [{"subject": "Alice", "fact": _ALICE_MOVE, "kind": "plan", "at_seconds": 0},
                  {"subject": "Q4 budget", "fact": "The Q4 budget cut is ten percent",
                   "kind": "other", "at_seconds": 32}]},
    "pasta-making cooking class": {
        "title": "Offsite idea: cooking class",
        "summary": "A pasta-making cooking class for the team offsite instead of the "
                   "escape room; ask Alice before she leaves for Berlin.",
        "key_points": ["Cooking school near the river"],
        "people": [ME, "Alice"], "topics": ["offsite"],
        "action_items": [{"text": "Ask Alice about a cooking class for the offsite",
                          "owner": ME, "at_seconds": 8}],
        "facts": [{"subject": "Alice", "fact": _ALICE_MOVE, "kind": "plan", "at_seconds": 8}]},
    "Casa Lupo is booked": {
        "kind": "casual", "title": "Birthday dinner plans",
        "summary": "Casa Lupo is booked for twelve guests on November 8 at 7 p.m.; the "
                   "user picks up Aunt Rosa from the station.",
        "key_points": ["Twelve guests", "Aunt Rosa comes by train"],
        "people": ["Maria", ME, "Aunt Rosa"], "topics": ["family", "birthday"],
        "action_items": [{"text": "Pick up Aunt Rosa from the train station", "owner": ME,
                          "kind": "event", "due": "November eighth", "at_seconds": 16}],
        "facts": [{"subject": "Mom", "fact": _MOM_70, "kind": "personal", "at_seconds": 0}]},
    "welcome to the nine o'clock news": {"discard": "tv_or_media"},
    "Your sourdough is ready": {
        "kind": "casual", "title": "At the bakery",
        "summary": "The rye bread is ready at four; the bakery is closed on Monday.",
        "key_points": ["Rye bread at four", "Closed on Monday"],
        "people": ["Speaker 1", ME], "topics": ["shopping"],
        "action_items": [{"text": "Pick up the rye bread at the bakery", "owner": ME,
                          "kind": "reminder", "time": "4 pm", "at_seconds": 8}],
        "facts": []},
    "Remind me tomorrow at 6 p.m.": {
        "title": "Call Mom",
        "summary": "Reminder to call Mom about the birthday dinner tomorrow at 6 p.m.",
        "key_points": [], "people": [ME, "Mom"], "topics": ["family"],
        "action_items": [{"text": "Call Mom about the birthday dinner", "kind": "reminder",
                          "due": "tomorrow at 6 p.m.", "time": "6 p.m.", "at_seconds": 0}],
        "facts": []},
    "relaunching the community newsletter": {
        "kind": "meeting", "title": "Community newsletter relaunch",
        "summary": "The newsletter shrinks to three sections and moves to Sunday mornings; "
                   "the first new issue goes out on November 15.",
        "key_points": ["Three sections and a photo of the month", "Sent on Sunday mornings"],
        "people": ["Priya Example", ME, "Lena Example", "Omar Example"],
        "topics": ["newsletter"],
        "action_items": [{"text": "Set up the new newsletter template in the mailing tool",
                          "owner": ME, "at_seconds": 41},
                         {"text": "Write the welcome email for new readers",
                          "owner": "Omar Example", "at_seconds": 55}],
        "facts": [{"subject": "Community newsletter", "fact": "The community newsletter "
                   "goes out on Sunday mornings", "kind": "decision", "at_seconds": 30}]},
    "Q4 budget for the community garden": {
        "kind": "meeting", "title": "Community garden budget",
        "summary": "The tool shed goes to the second supplier for about 3200 euros; the "
                   "user confirms the order by Friday.",
        "key_points": ["Second supplier is cheaper"],
        "people": ["Sam Example", ME, "Kai Example", "Dana Example"], "topics": ["garden"],
        "action_items": [{"text": "Confirm the tool shed order with the second supplier",
                          "owner": ME, "due": "Friday", "at_seconds": 38}],
        "facts": [{"subject": "Community garden", "fact": "The community garden buys its "
                   "tool shed from the second supplier", "kind": "decision",
                   "at_seconds": 38}]},
    "Buy oat milk and coffee beans": {
        "title": "Weekend errands",
        "summary": "Buy oat milk and coffee beans, and ask Maria which cake Mom likes.",
        "key_points": ["Oat milk", "Coffee beans"], "people": [ME, "Maria", "Mom"],
        "topics": ["errands"],
        "action_items": [{"text": "Buy oat milk and coffee beans", "owner": ME,
                          "list_name": "Groceries", "at_seconds": 1},
                         {"text": "Ask Maria which cake Mom likes", "owner": ME,
                          "at_seconds": 2}],
        "facts": []},
}

# fact text (new) -> fact text (known) it updates
UPDATES = {"The Q4 budget cut is ten percent": "The Q4 budget cut is fifteen percent"}


def _canned(user: str) -> dict[str, Any]:
    for marker, out in CANNED.items():
        if marker in user:
            return out
    raise AssertionError(f"no canned summary for: {user[:200]!r}")


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def fake_llm(request: httpx.Request) -> httpx.Response:
    """llama-server stand-in: answers each structured call from ``CANNED``."""
    if request.method == "GET":  # /models, for the settings page
        return httpx.Response(200, json={"data": [{"id": "demo-model"},
                                                  {"id": "demo-model-large"}]})
    body = json.loads(request.content)
    name = body["response_format"]["json_schema"]["name"]
    user = body["messages"][-1]["content"]
    if name == "Triage":
        c = _canned(user)
        content: Any = ({"discard": True, "reason": c["discard"], "kind": "other"}
                        if "discard" in c else
                        {"discard": False, "reason": "none", "kind": c.get("kind", "other")})
    elif name == "SummaryNotes":
        c = _canned(user)
        content = {k: c[k] for k in ("title", "summary", "key_points", "people", "topics")}
    elif name == "ActionItemList":
        content = {"action_items": _canned(user)["action_items"]}
    elif name == "FactList":
        content = {"facts": _canned(user)["facts"]}
    elif name == "FactReview":
        content = {"scores": [{"number": f["number"], "score": 5}
                              for f in json.loads(user)["facts"]]}
    elif name == "FactLinks":
        asked = json.loads(user)
        links = []
        for f in asked["new"]:
            for k in asked["known"]:
                if _norm(f["fact"]) == _norm(k["fact"]):
                    links.append({"new": f["number"], "relation": "same", "existing": k["id"]})
                    break
                if UPDATES.get(f["fact"]) == k["fact"]:
                    links.append({"new": f["number"], "relation": "updates",
                                  "existing": k["id"]})
                    break
            else:
                links.append({"new": f["number"], "relation": "new", "existing": None})
        content = {"links": links}
    elif name == "DailyDigest":
        notes = json.loads(user)
        content = {"headline": f"{len(notes)} recording(s): "
                               + "; ".join(n["title"] for n in notes),
                   "summary": " ".join(n["summary"] for n in notes),
                   "highlights": [k for n in notes for k in n["key_points"]][:4],
                   "open_action_items": [a for n in notes for a in n["action_items"]],
                   "people": sorted({p for n in notes for p in n["people"]} - {ME})}
    else:
        raise AssertionError(f"unexpected structured call {name}")
    return httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps(content)}}]})


def make_pipeline(settings: Settings, store: Store) -> Pipeline:
    llm = LLMClient(settings.llm_base_url, settings.llm_model,
                    transport=httpx.MockTransport(fake_llm))
    return Pipeline(settings, store, EnergyVAD(),
                    summarizer=Summarizer(llm, ME, timezone=settings.timezone,
                                          digest_skip_lists=settings.digest_skip_lists))


def _ts(start: str) -> float:
    return datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()


def _insert_session(store: Store, sid: str, device: str, t0: float, end: float,
                    kind: str) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                  " last_speech_at, received_at, status, stop_reason, kind)"
                  " VALUES (?, ?, ?, ?, ?, ?, 'processing', 'demo', ?)",
                  (sid, device, t0, end, end, time.time(), kind))


def _insert_conversation(store: Store, pipeline: Pipeline, key: str, start: str,
                         device: str, lines: list[tuple[str, str]]) -> list[str]:
    sid, t0 = f"demo-{key}", _ts(start)
    note = key.startswith("note-")
    segs = [Segment(t0 + 8 * i, t0 + 8 * i + 7, text, speaker, speaker == ME)
            for i, (speaker, text) in enumerate(lines)]
    end = segs[-1].ended_at
    _insert_session(store, sid, device, t0, end, NOTE if note else CONVERSATION)
    store.replace_segments(sid, device, segs, [(t0, end + 1)])
    unnamed = sorted({s for s, _ in lines if s.startswith("Speaker ")})
    if unnamed:  # what diarization leaves for voices it could not name
        store.put_speaker_candidates(sid, {label: voice(10 + i)
                                           for i, label in enumerate(unnamed)})
    conv_ids = (pipeline._assign_note if note else pipeline._assign_conversations)(sid, device)
    store.set_session_status(sid, "done")
    return conv_ids


def _insert_meeting(store: Store, pipeline: Pipeline, key: str, start: str,
                    lines: list[tuple[str, str, float, float]]) -> list[str]:
    """As seed_demo.insert_meeting: per-track lines, then the bot's chat call."""
    from athena.meetings.ingest.chat import ChatIn, ingest_chat

    sid, device, t0 = f"demo-{key}", "zoom", _ts(start)
    segs = sorted((Segment(t0 + at, t0 + at + length, text, speaker, speaker == ME)
                   for speaker, text, at, length in lines), key=lambda s: s.started_at)
    end = max(s.ended_at for s in segs)
    _insert_session(store, sid, device, t0, end, CONVERSATION)
    store.replace_segments(sid, device, segs, [(t0, end + 1)])
    conv_ids = pipeline._assign_conversations(sid, device)
    store.set_session_status(sid, "done")
    topic, people, chat = MEETING_CHAT[key]
    ids = {name: f"1678{i:04d}" for i, name in enumerate(people)}
    body = ChatIn.model_validate({
        "topic": topic,
        "participants": [{"zoom_user_id": ids[n], "name": n, "joined_at": t0,
                          "left_at": end} for n in people],
        "lines": [{"at": t0 + at, "from": n, "zoom_user_id": ids[n], "text": text}
                  for n, text, at in chat]})
    out = ingest_chat(pipeline, sid, body)
    return [out["conversation_id"]] if out["conversation_id"] else conv_ids


DIGEST_DATES = ("2026-09-15", "2026-10-14")


def build(data_dir: Path) -> None:
    """Write the contract store into ``data_dir`` (convo.db), with the clock and ids
    fixed. Fails if a summary fails."""
    with pytest.MonkeyPatch.context() as mp:
        frozen(mp)
        settings = settings_for(data_dir)
        store = Store(settings.db_path)
        store.put_speaker(ME, voice(0), is_owner=True)
        store.put_speaker("Alice", voice(1), is_owner=False)
        pipeline = make_pipeline(settings, store)
        entries: list[tuple[str, list[str]]] = []
        recordings = sorted(
            [("c", key, start, device, lines) for key, start, device, lines in CONVERSATIONS]
            + [("m", key, start, "zoom", lines) for key, start, lines in MEETINGS],
            key=lambda r: r[2])
        for what, key, start, device, lines in recordings:
            conv_ids = (_insert_meeting(store, pipeline, key, start, lines) if what == "m"
                        else _insert_conversation(store, pipeline, key, start, device, lines))
            entries.append((key, conv_ids))
        note = pipeline.ingest_text(
            source_id=WRITTEN_NOTE["source_id"], text=WRITTEN_NOTE["text"],
            written_at=datetime.fromisoformat(WRITTEN_NOTE["written_at"]).timestamp(),
            link=WRITTEN_NOTE["link"], title=WRITTEN_NOTE["title"])
        entries.append(("written-note", [note["conversation_id"]]))
        for key, conv_ids in entries:
            for cid in conv_ids:
                assert pipeline.summarize_conversation(cid), f"summary failed: {key}"
        for date in DIGEST_DATES:
            assert pipeline.make_digest(date) is not None, date
        store.close()
