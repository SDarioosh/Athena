#!/usr/bin/env python3
"""Seed a store with fictional, speaker-labelled conversations for frontend evaluation.

Transcripts are inserted directly (no audio); summaries, action items and facts are then
produced by the real llama-server pipeline. All people and events are made up.

    CONVO_INGEST_TOKEN=... CONVO_DATA_DIR=/tmp/demo CONVO_LLM_BASE_URL=... \
        python scripts/seed_demo.py
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

from convo_server.config import get_settings
from convo_server.llm import LLMClient, Summarizer
from convo_server.pipeline import Pipeline
from convo_server.store import CONVERSATION, NOTE, Segment, Store
from convo_server.vad import EnergyVAD

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


def main() -> int:
    settings = get_settings()
    store = Store(settings.db_path)
    llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                    settings.llm_timeout_seconds)
    pipeline = Pipeline(settings, store, EnergyVAD(),
                        summarizer=Summarizer(llm, ME, timezone=settings.timezone))
    failed = 0
    for key, start, device, lines in CONVERSATIONS:
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
    if failed:
        print(f"{failed} summaries failed; run again to retry them.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
