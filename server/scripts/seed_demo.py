#!/usr/bin/env python3
"""Seed a store with fictional, speaker-labelled conversations for frontend evaluation.

Transcripts are inserted directly (no audio); summaries, action items and facts are then
produced by the real llama-server pipeline. All people and events are made up.

    CONVO_INGEST_TOKEN=... CONVO_DATA_DIR=/tmp/demo CONVO_LLM_BASE_URL=... \
        python scripts/seed_demo.py [--texts]

``--texts`` also adds fictional text messages (docs/messages.md): two chats, stored
through the same path the messages relay uses, each thread summarized.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

from athena.config import get_settings
from athena.db.store import Store
from athena.domain import CONVERSATION, NOTE, Segment
from athena.pipeline import Pipeline
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from athena.vad import EnergyVAD

ME = "Me"

# (key, start UTC, device, [(speaker, text), ...]); lines are ~8 s apart. Keys starting
# with "note-" are voice notes (the owner talking to themselves, session kind NOTE).
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
    # A TV left on: should be discarded, not summarized (no answers come from it).
    ("tv-news", "2026-10-17T21:00", "omi", [
        ("Speaker 1", "Good evening, and welcome to the nine o'clock news."),
        ("Speaker 1", "Tonight's top story: heavy rain is expected across the northern region over the weekend."),
        ("Speaker 2", "In sport, the home side won three to one after a late goal in the final minutes."),
        ("Speaker 1", "Stay with us after the break for the full weather forecast."),
    ]),
]

# Zoom meetings (device "zoom"): (key, start UTC, [(speaker, text, at seconds, length)]).
# Each line is one participant's own track, named by their Zoom name, so lines may
# overlap: at 0:30 Omar talks over Lena (crosstalk), and both lines are kept.
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

# What the Zoom bot sends when a meeting ends (POST /v1/sessions/{id}/chat): the topic,
# the participant list (Rafael never speaks or types) and chat messages
# ((sender, text, at seconds)). Dana only types in chat.
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


def insert_meeting(store: Store, pipeline: Pipeline, key: str, start: str,
                   lines: list[tuple[str, str, float, float]]) -> list[str]:
    """Insert one demo meeting's lines as the per-track pipeline stores them; returns
    its conversation ids."""
    sid, device = f"demo-{key}", "zoom"
    t0 = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
    segs = sorted((Segment(t0 + at, t0 + at + length, text, speaker, speaker == ME)
                   for speaker, text, at, length in lines), key=lambda s: s.started_at)
    end = max(s.ended_at for s in segs)
    with store.tx() as c:
        c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                  " last_speech_at, received_at, status, stop_reason, kind)"
                  " VALUES (?, ?, ?, ?, ?, ?, 'processing', 'demo', ?)",
                  (sid, device, t0, end, end, time.time(), CONVERSATION))
    store.replace_segments(sid, device, segs, [(t0, end + 1)])
    conv_ids = pipeline._assign_conversations(sid, device)
    store.set_session_status(sid, "done")
    if key in MEETING_CHAT:  # as the bot sends it after the meeting
        from athena.meetings.ingest.chat import ChatIn, ingest_chat

        topic, people, chat = MEETING_CHAT[key]
        ids = {name: f"1678{i:04d}" for i, name in enumerate(people)}
        body = ChatIn.model_validate({
            "topic": topic,
            "participants": [{"zoom_user_id": ids[n], "name": n, "joined_at": t0,
                              "left_at": end} for n in people],
            "lines": [{"at": t0 + at, "from": n, "zoom_user_id": ids[n], "text": text}
                      for n, text, at in chat]})
        out = ingest_chat(pipeline, sid, body)
        conv_ids = [out["conversation_id"]] if out["conversation_id"] else conv_ids
    return conv_ids


# Fictional text messages (--texts): (chat id, group name or None, [(minutes after the
# chat's start, sender handle or None for the owner, text)]), uploaded like the relay does.
TEXT_START = "2026-10-12T17:30"
TEXT_HANDLES = {"+15550100": "Jonas", "+15550123": "Lena Example"}
TEXT_CHATS: list[tuple[str, str | None, list[tuple[int, str | None, str]]]] = [
    ("iMessage;-;+15550100", None, [
        (0, "+15550100", "Are we still on for the long run on Sunday?"),
        (2, None, "Yes! 7am at the river path?"),
        (3, "+15550100", "Perfect. Can you bring the spare running belt? Mine broke."),
        (5, None, "Sure, I'll bring it."),
        (1440, "+15550100", "Also my sister moves to Porto next month, so I'll be driving "
                            "down there a lot."),
    ]),
    ("iMessage;+;chat555", "Newsletter crew", [
        (60, "+15550123", "Draft of the November issue is in the shared folder."),
        (62, None, "Thanks Lena, I'll review it by Thursday."),
        (64, "+15550123", "Great. Printing deadline is Friday the 14th."),
    ]),
]


def insert_texts(pipeline: Pipeline) -> list[str]:
    """Upload the fictional chats; returns the thread ids to summarize."""
    t0 = datetime.fromisoformat(TEXT_START).replace(tzinfo=UTC).timestamp()
    messages = [{"id": f"demo-{n}-{i}", "chat": chat, "at": t0 + minutes * 60,
                 "from_me": handle is None, "handle": handle, "text": text}
                for n, (chat, _, lines) in enumerate(TEXT_CHATS)
                for i, (minutes, handle, text) in enumerate(lines)]
    chats = [{"id": chat, "name": name, "group": name is not None,
              "handles": sorted({h for _, h, _ in lines if h})}
             for chat, name, lines in TEXT_CHATS]
    pipeline.ingest_messages({"source": "imessage", "chats": chats, "messages": messages,
                              "handles": [{"handle": h, "name": n}
                                          for h, n in TEXT_HANDLES.items()]})
    return [c["id"] for c in pipeline.store.list_conversations(kind="text", limit=100)
            if c["status"] not in ("summarized", "discarded")]

def main() -> int:
    settings = get_settings()
    store = Store(settings.db_path)
    llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                    settings.llm_timeout_seconds)
    pipeline = Pipeline(settings, store, EnergyVAD(),
                        summarizer=Summarizer(llm, ME, timezone=settings.timezone,
                                                      digest_skip_lists=settings.digest_skip_lists))
    failed = 0
    meetings = {key: (start, lines) for key, start, lines in MEETINGS}
    entries = [*CONVERSATIONS, *((key, start, "zoom", []) for key, start, _ in MEETINGS)]
    for key, start, device, lines in entries:
        sid = f"demo-{key}"
        if store.get_session(sid):
            # Already inserted; only retry summaries that failed on an earlier run.
            conv_ids = sorted({s.conversation_id for s in store.session_segments(sid)
                               if s.conversation_id})
            conv_ids = [c for c in conv_ids
                        if store.get_conversation(c)["status"] not in ("summarized", "discarded")]
            if not conv_ids:
                print("exists:", sid)
                continue
        elif key in meetings:
            conv_ids = insert_meeting(store, pipeline, key, *meetings[key])
        else:
            t0 = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
            segs = [Segment(t0 + 8 * i, t0 + 8 * i + 7, text, speaker, speaker == ME)
                    for i, (speaker, text) in enumerate(lines)]
            with store.tx() as c:
                end = segs[-1].ended_at
                c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                          " last_speech_at, received_at, status, stop_reason, kind)"
                          " VALUES (?, ?, ?, ?, ?, ?, 'processing', 'demo', ?)",
                          (sid, device, t0, end, end, time.time(),
                           NOTE if key.startswith("note-") else CONVERSATION))
            store.replace_segments(sid, device, segs, [(t0, end + 1)])
            assign = (pipeline._assign_note if key.startswith("note-")
                      else pipeline._assign_conversations)
            conv_ids = assign(sid, device)
            store.set_session_status(sid, "done")
        for cid in conv_ids:
            t = time.time()
            ok = pipeline.summarize_conversation(cid)
            failed += not ok
            conv = store.get_conversation(cid)
            print(f"{key}: {cid} summarized={ok} ({time.time() - t:.1f}s) {conv['title']!r}"
                  + (f" discarded: {conv['discard_reason']}" if conv["status"] == "discarded"
                     else ""))
    if "--texts" in sys.argv[1:]:
        for cid in insert_texts(pipeline):
            ok = pipeline.summarize_conversation(cid)
            failed += not ok
            print(f"texts: {cid} summarized={ok} "
                  f"{store.get_conversation(cid)['title']!r}")
    if failed:
        print(f"{failed} summaries failed; run again to retry them.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
