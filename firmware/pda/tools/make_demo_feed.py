#!/usr/bin/env python3
"""Write the PDA's fictional demo feed and details (docs/t5-pda-plan.md §4.1, §4.3).

The people and events mirror server/scripts/seed_demo.py (Alice, Maria, Dr. Patel, Tom,
Ben, Jonas), already summarized, as the phone would build them from the server's API.
Everything is made up. Output (committed, used by the host tests and the console loader):

    firmware/pda/test/fixtures/feed_demo.json
    firmware/pda/test/fixtures/feed_insight_demo.json  (the same day with the hub's insight)
    firmware/pda/test/fixtures/detail_conv_demo.json
    firmware/pda/test/fixtures/detail_transcript_demo.json

    python firmware/pda/tools/make_demo_feed.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "test" / "fixtures"

# 2026-10-20 07:00 UTC, a Tuesday morning in Europe/Berlin (UTC+2 until 25 Oct).
GENERATED = "2026-10-20T07:00:00Z"
VERSION = 1792479600  # epoch seconds of GENERATED

FEED = {
    "v": 1,
    "version": VERSION,
    "generated": GENERATED,
    "tz": "Europe/Berlin",
    "briefing": {
        "date": "2026-10-19",
        "text": (
            "A quiet Monday. Maria confirmed the dinner for Mom’s 70th at Casa Lupo — "
            "twelve guests, 7 p.m. on 8 November — and asked you to pick up Aunt Rosa from "
            "the station. The client demo is on Friday; Ben is worried about database load."
        ),
        "highlights": [
            "Dentist follow-up tomorrow at 09:00 (crown on the lower left molar)",
            "Client demo moved to Friday 23 Oct",
            "Lease renewal due 31 Oct: rent goes from €1,450 to €1,520",
            "Alice moves to Berlin on 12 January; you take over the vendor contracts",
        ],
    },
    "conversations": [
        {
            "id": "c_demo_maria_oct",
            "title": "Dinner for Mom’s 70th",
            "at": "2026-10-16T20:00:00Z",
            "people": ["Maria"],
            "summary": (
                "Maria booked Casa Lupo for 7 p.m. on 8 November, twelve guests. Aunt Rosa "
                "comes by train; you’ll pick her up from the station that afternoon."
            ),
            "points": ["Casa Lupo, 8 Nov, 19:00, 12 guests", "Pick up Aunt Rosa from the station"],
        },
        {
            "id": "c_demo_alice_1on1",
            "title": "1:1 with Alice: Berlin move and vendor contracts",
            "at": "2026-10-14T15:00:00Z",
            "people": ["Alice"],
            "summary": (
                "Alice’s move to the Berlin office is set for 12 January. You agreed to take "
                "over her vendor contracts. The Q4 budget cut is now 10 %, not 15 %."
            ),
            "points": [
                "Berlin move: 12 Jan",
                "You take over vendor contracts",
                "Budget cut now 10 %",
            ],
        },
        {
            "id": "c_demo_jonas_dinner",
            "title": "Dinner with Jonas",
            "at": "2026-10-10T18:00:00Z",
            "people": ["Jonas"],
            "summary": (
                "Jonas signed up for the Lisbon marathon in December and runs 30 km on Sundays. "
                "He recommended The Overstory. He is allergic to peanuts."
            ),
            "points": [
                "Lisbon marathon in December",
                "Book tip: The Overstory",
                "Allergic to peanuts",
            ],
        },
        {
            "id": "c_demo_standup",
            "title": "Stand-up: API migration done, demo moved",
            "at": "2026-10-07T09:30:00Z",
            "people": ["Ben", "Alice"],
            "summary": (
                "Ben finished the API migration. The client demo moved to 23 October. Ben is "
                "worried about database load from the new queries; you’ll review his pull request."
            ),
            "points": ["Demo on 23 Oct", "Review Ben’s PR", "Check database load"],
        },
        {
            "id": "c_demo_landlord",
            "title": "Lease renewal with Tom",
            "at": "2026-10-03T11:00:00Z",
            "people": ["Tom"],
            "summary": (
                "Rent rises from €1,450 to €1,520 from January. Tom needs the signed renewal by "
                "31 October and will fix the bathroom fan next week."
            ),
            "points": [
                "Rent €1,520 from January",
                "Signed renewal due 31 Oct",
                "Tom fixes the bathroom fan",
            ],
        },
    ],
    "actions": [
        {
            "id": 14,
            "text": "Review Ben’s pull request",
            "owner": "Me",
            "due": "2026-10-07",
            "conv": "c_demo_standup",
        },
        {
            "id": 15,
            "text": "Check database load before the client demo",
            "owner": "Me",
            "due": "2026-10-23",
            "conv": "c_demo_standup",
        },
        {
            "id": 12,
            "text": "Sign and return the lease renewal to Tom",
            "owner": "Me",
            "due": "2026-10-31",
            "conv": "c_demo_landlord",
        },
        {
            "id": 11,
            "text": "Get the photo album for Mom’s 70th",
            "owner": "Me",
            "due": "2026-11-08",
            "conv": "c_demo_maria_oct",
        },
        {
            "id": 18,
            "text": "Pick up Aunt Rosa from the train station",
            "owner": "Me",
            "due": "2026-11-08",
            "conv": "c_demo_maria_oct",
        },
        {
            "id": 17,
            "text": "Take over Alice’s vendor contracts",
            "owner": "Me",
            "conv": "c_demo_alice_1on1",
        },
        {"id": 16, "text": "Fix the bathroom fan", "owner": "Tom", "conv": "c_demo_landlord"},
    ],
    "calendar": [
        {
            "id": "ev_demo_1",
            "title": "Team stand-up",
            "start": "2026-10-20T07:30:00Z",
            "end": "2026-10-20T07:45:00Z",
            "where": "Room 2.14",
            "all_day": False,
        },
        {
            "id": "ev_demo_2",
            "title": "Dentist follow-up (Dr. Patel)",
            "start": "2026-10-21T07:00:00Z",
            "end": "2026-10-21T07:45:00Z",
            "where": "Praxis Patel",
            "all_day": False,
        },
        {
            "id": "ev_demo_3",
            "title": "Client demo",
            "start": "2026-10-23T12:00:00Z",
            "end": "2026-10-23T13:00:00Z",
            "where": "Video call",
            "all_day": False,
        },
        {
            "id": "ev_demo_4",
            "title": "Ben’s PR review session",
            "start": "2026-10-22T13:00:00Z",
            "end": "2026-10-22T13:30:00Z",
            "all_day": False,
        },
        {
            "id": "ev_demo_5",
            "title": "Jonas: long run (cheer at km 20)",
            "start": "2026-10-25",
            "all_day": True,
        },
        {
            "id": "ev_demo_6",
            "title": "Lease renewal reminder",
            "start": "2026-10-26",
            "all_day": True,
        },
    ],
    # The owner's Odysseus checklists (plan §4.4), pinned first. `i` is the hub's index in
    # the note's item list: gaps (2 is gone) must survive.
    "todos": [
        {"id": "note_demo_groceries", "title": "Groceries", "pinned": True, "due": None,
         "items": [{"i": 0, "text": "Milk", "done": False},
                   {"i": 1, "text": "Bergkäse für Freitag", "done": True},
                   {"i": 3, "text": "Coffee beans", "done": False}]},
        {"id": "note_demo_mom70", "title": "Mom’s 70th — dinner at Casa Lupo", "pinned": False,
         "due": "2026-11-08",
         "items": [{"i": 0, "text": "Photo album: pick 40 old family pictures", "done": False},
                   {"i": 1, "text": "Pick up Aunt Rosa from the station (afternoon)",
                    "done": False},
                   {"i": 2, "text": "Confirm twelve guests with Maria", "done": True}]},
        {"id": "note_demo_untitled", "title": None, "pinned": False, "due": None,
         "items": [{"i": 0, "text": "Return the library book", "done": False}]},
    ],
    # Event titles for the "+" picker, configured in the phone app.
    "templates": ["Call", "Meeting", "Reminder", "Focus time"],
}

# The same morning with more on the lists and the calendar, and the hub's "Today insight"
# (plan §4.1 `insight`). It also carries what the T5 must survive: a ref to a note that
# isn't in the feed, an item without `why`, a section that isn't a list, an event that
# is already over (08:00 local, the feed is from 09:00) and an unknown key.
FEED_INSIGHT = copy.deepcopy(FEED)
FEED_INSIGHT["calendar"] += [
    {
        "id": "ev_demo_call_tom",
        "title": "Call Tom about the bathroom fan",
        "start": "2026-10-20T06:00:00Z",
        "end": "2026-10-20T06:15:00Z",
        "all_day": False,
    },
    {
        "id": "ev_demo_lunch",
        "title": "Lunch with Maria",
        "start": "2026-10-20T10:30:00Z",
        "end": "2026-10-20T11:30:00Z",
        "where": "Café Mira",
        "all_day": False,
    },
    {
        "id": "ev_demo_alice",
        "title": "1:1 with Alice",
        "start": "2026-10-20T14:00:00Z",
        "end": "2026-10-20T14:30:00Z",
        "where": "Room 3.02",
        "all_day": False,
    },
]
FEED_INSIGHT["calendar"].sort(key=lambda e: e["start"])
FEED_INSIGHT["todos"] = [
    FEED["todos"][0],  # Groceries, pinned: a shopping list, so not on Today
    {"id": "note_demo_house", "title": "House", "pinned": True, "due": None,
     "items": [{"i": 0, "text": "Ask Tom when he fixes the bathroom fan", "done": False},
               {"i": 1, "text": "Water the plants", "done": False}]},
    FEED["todos"][1],  # Mom's 70th, due 8 Nov
    {"id": "note_demo_work", "title": "Work", "pinned": False, "due": "2026-10-19",
     "items": [{"i": 0, "text": "Send the Q4 budget sheet to Alice", "done": False},
               {"i": 1, "text": "Book the Berlin trip for January", "done": False},
               {"i": 2, "text": "Expense report for September", "done": True}]},
    {"id": "note_demo_demo", "title": "Client demo prep", "pinned": False,
     "due": "2026-10-20",
     "items": [{"i": 0, "text": "Rehearse the demo end to end", "done": False},
               {"i": 1, "text": "Check database load with Ben’s new queries",
                "done": False}]},
    FEED["todos"][2],  # untitled, undated
    {"id": "note_demo_shopping", "title": "Shopping list", "pinned": False, "due": None,
     "items": [{"i": 0, "text": "AA batteries", "done": False}]},
]
FEED_INSIGHT["insight"] = {
    "date": "2026-10-20",
    "headline": (
        "Friday’s client demo is the week’s big item: rehearse it this morning while the "
        "calendar is clear, and send Alice the Q4 budget, a day late. Lunch with Maria "
        "settles the seating plan."
    ),
    "focus": [
        {"ref": "todo:note_demo_work:0", "why": "a day late; Alice needs it for the Q4 plan"},
        {"ref": "todo:note_demo_demo:0", "why": "the morning is clear until the stand-up"},
        {"ref": "event:ev_demo_lunch", "why": "Maria wants the seating plan settled"},
        {"ref": "event:ev_demo_call_tom", "why": "the fan again"},
        {"ref": "todo:note_demo_gone:2", "why": "a note that isn't in the feed"},
    ],
    "coming_up": [
        {"ref": "event:ev_demo_2", "why": "bring the insurance card"},
        {"ref": "event:ev_demo_3", "why": "check database load before it"},
        {"ref": "todo:note_demo_mom70:1"},
    ],
    "also": {"ref": "todo:note_demo_house:1", "why": "not a list: ignored"},
    "tone": "calm",
}

DETAIL_CONV = {
    "v": 1,
    "ref": "conv:c_demo_alice_1on1",
    "title": "1:1 with Alice: Berlin move and vendor contracts",
    "at": "2026-10-14T15:00:00Z",
    "people": ["Alice"],
    "summary": FEED["conversations"][1]["summary"],
    "points": FEED["conversations"][1]["points"],
    "actions": [
        {
            "id": 17,
            "text": "Take over Alice’s vendor contracts",
            "owner": "Me",
            "due": "",
            "status": "open",
        }
    ],
}

# The same lines as seed_demo.py's "alice-1on1", ~8 s apart.
ALICE_LINES = [
    ("Alice", "It's official, my Berlin move date is January twelfth."),
    ("Me", "So soon. Who takes over your vendor contracts?"),
    ("Alice", "I'd like you to take over the vendor contracts. Can you?"),
    ("Me", "Yes, I'll take over the vendor contracts."),
    ("Alice", "Thank you. And good news on the budget: the cut is now ten percent, not fifteen."),
]

DETAIL_TRANSCRIPT = {
    "v": 1,
    "ref": "transcript:c_demo_alice_1on1@0-600",
    "title": "1:1 with Alice: Berlin move and vendor contracts",
    "from": 0,
    "to": 600,
    "next": None,
    "segments": [
        {"o": 8.0 * i, "s": s, "me": s == "Me", "x": x} for i, (s, x) in enumerate(ALICE_LINES)
    ],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, doc in (
        ("feed_demo.json", FEED),
        ("feed_insight_demo.json", FEED_INSIGHT),
        ("detail_conv_demo.json", DETAIL_CONV),
        ("detail_transcript_demo.json", DETAIL_TRANSCRIPT),
    ):
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")
    for name in ("feed_demo.json", "feed_insight_demo.json"):
        assert len((OUT / name).read_bytes()) < 32 * 1024


if __name__ == "__main__":
    main()
