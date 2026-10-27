#!/usr/bin/env python3
"""Replay a fictional Zoom meeting against a running server and time the end of it.

Uploads one 30 s Opus chunk per participant per round, as the meeting bot does
(``device=zoom``, ``track`` / ``track_name``), paced like a live meeting, then ends the
session and reports how long the server took from ``/end`` to the meeting's lines
(session ``done``) and to its summary (conversation ``summarized``). Use it to measure
live transcription (``CONVO_MEETING_LIVE_TRANSCRIBE``, lane 5D) on real hardware.

The voices are fictional: the lines of a meeting in ``seed_demo.py`` spoken by
``espeak-ng`` (one voice per participant), repeated to the length you ask for. Or bring
your own fictional recordings with ``--track "Name=file.wav"`` (any format ffmpeg reads;
one file per participant, all starting at the meeting's start).

Usage:
    python scripts/meeting_replay.py --url http://127.0.0.1:8000 \\
        --token "$CONVO_INGEST_TOKEN" --minutes 60 --people 4
    python scripts/meeting_replay.py --token ... --track "Ana Example=ana.wav" \\
        --track "Bo Example=bo.wav"
    python scripts/meeting_replay.py --token ... --watch <session id>   # a real meeting

``--speed 4`` replays four times faster than real time (the server then has less time
per round to keep up, so the end is a little slower than in a real meeting). Needs
stdlib, httpx, ffmpeg, and espeak-ng for the built-in script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

SR = 16_000
CHUNK = 30
VOICES = ["en-us+m3", "en-gb+f2", "en-us+m7", "en-gb+f4", "en-us+f3", "en-gb+m2"]
EXTRA = ["Kai Example", "Noor Example", "Ravi Example"]  # more fictional people


def pcm_of(cmd: list[str], data: bytes | None = None) -> bytes:
    return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout


def decode(path: str) -> bytes:
    return pcm_of(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le",
                   "-ac", "1", "-ar", str(SR), "pipe:1"])


def speak(text: str, voice: str) -> bytes:
    wav = pcm_of(["espeak-ng", "-v", voice, "-s", "165", "--stdout", text])
    return pcm_of(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-f",
                   "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"], wav)


def to_opus(pcm: bytes) -> bytes:
    return pcm_of(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "s16le", "-ar", str(SR),
                   "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg",
                   "pipe:1"], pcm)


def scripted(minutes: float, people: int, owner: str) -> dict[str, bytearray]:
    """Each participant's whole track: the seed_demo newsletter meeting's lines, spoken
    by one voice each, repeated until the meeting is ``minutes`` long. Silence
    elsewhere; people take turns (one line overlaps another, as in the seed)."""
    from seed_demo import ME, MEETINGS

    _, _, lines = next(m for m in MEETINGS if m[0] == "zoom-newsletter")
    names = list(dict.fromkeys(owner if s == ME else s for s, *_ in lines))
    names = (names + EXTRA)[:max(1, people)]
    length = int(minutes * 60 * SR) * 2
    tracks = {n: bytearray(length) for n in names}
    cache: dict[tuple[str, str], bytes] = {}
    period = max(start + dur for _, _, start, dur in lines) + 4
    rnd = 0
    while (rnd * period) * SR * 2 < length:
        for i, (speaker, text, start, _) in enumerate(lines):
            who = owner if speaker == ME else speaker
            if who not in tracks:  # fewer people asked: give the line to someone else
                who = names[(i + rnd) % len(names)]
            key = (who, text)
            if key not in cache:
                cache[key] = speak(text, VOICES[names.index(who) % len(VOICES)])
            at = int((rnd * period + start) * SR) * 2
            pcm = cache[key][:max(0, length - at)]
            tracks[who][at:at + len(pcm)] = pcm
        rnd += 1
    return tracks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--people", type=int, default=4)
    ap.add_argument("--owner", default="Alex Example",
                    help="the owner's Zoom name (match it with CONVO_ZOOM_OWNER_NAMES)")
    ap.add_argument("--track", action="append", default=[], metavar="NAME=FILE")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--session", default=None)
    ap.add_argument("--watch", metavar="SESSION_ID",
                    help="upload nothing: time a real meeting's session from now (its end)")
    ap.add_argument("--lines-only", action="store_true",
                    help="stop when the lines are ready (a server without an LLM)")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="seconds to wait for the summary after /end")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"}
    if args.watch:  # a real meeting: time from now (its end) to the lines and summary
        with httpx.Client(base_url=args.url, headers=headers, timeout=120) as http:
            return wait(http, args.watch, time.time(), args)

    if args.track:
        tracks = {}
        for spec in args.track:
            name, _, path = spec.partition("=")
            tracks[name.strip()] = bytearray(decode(path))
        length = max(len(t) for t in tracks.values())
        for t in tracks.values():
            t.extend(bytes(length - len(t)))
    else:
        print(f"speaking a {args.minutes:g}-minute fictional meeting with espeak-ng ...")
        tracks = scripted(args.minutes, args.people, args.owner)
        length = len(next(iter(tracks.values())))
    step = CHUNK * SR * 2
    rounds = (length + step - 1) // step
    ids = {name: str(16778240 + 1024 * i) for i, name in enumerate(tracks)}  # Zoom-like
    session = args.session or f"replay-{uuid.uuid4().hex[:8]}"
    start = time.time()
    print(f"session {session}: {len(tracks)} tracks, {rounds} rounds of {CHUNK} s, "
          f"speed {args.speed:g}")
    with httpx.Client(base_url=args.url, headers=headers, timeout=120) as http:
        for seq in range(rounds):
            # round seq's audio is complete at start + (seq + 1) * CHUNK, as for the bot
            pause = start + (seq + 1) * CHUNK / args.speed - time.time()
            if pause > 0:
                time.sleep(pause)
            for name, pcm in tracks.items():
                part = bytes(pcm[seq * step:(seq + 1) * step])
                r = http.post("/v1/ingest/chunk",
                              files={"audio": (f"{seq}.opus", to_opus(part), "audio/ogg")},
                              data={"device": "zoom", "session_id": session,
                                    "sequence": str(seq), "codec": "opus",
                                    "timestamp": str(start + seq * CHUNK),
                                    "track": ids[name], "track_name": name})
                r.raise_for_status()
            print(f"round {seq + 1}/{rounds} uploaded", flush=True)
        ended = time.time()
        http.post(f"/v1/sessions/{session}/end").raise_for_status()
        return wait(http, session, ended, args)


def wait(http: httpx.Client, session: str, ended: float, args: argparse.Namespace) -> int:
    """Poll until the session's lines are stored (status done) and its conversation is
    summarized; print both times from ``ended``."""
    print(f"session {session} ended; waiting for the lines and the summary ...", flush=True)
    sess = http.get(f"/v1/sessions/{session}")
    sess.raise_for_status()
    since = sess.json()["started_at"]
    done_at = None
    while time.time() - ended < args.timeout:
        if done_at is None:
            status = http.get(f"/v1/sessions/{session}").json()["status"]
            if status == "failed":
                print("processing failed; see the server log")
                return 1
            if status == "done":
                done_at = time.time()
                print(f"lines ready {done_at - ended:.1f} s after /end", flush=True)
                if args.lines_only:
                    return 0
        if done_at is not None:
            convs = http.get("/v1/conversations", params={"since": since}).json()
            zoom = sorted((c for c in convs["conversations"] if c.get("device") == "zoom"),
                          key=lambda c: c["started_at"])
            if zoom and zoom[0]["status"] in ("summarized", "discarded", "failed"):
                conv = zoom[0]
                print(f"summary ({conv['status']}) {time.time() - ended:.1f} s after /end: "
                      f"{conv.get('title')!r}")
                return 0 if conv["status"] == "summarized" else 1
        time.sleep(1.0)
    print(f"no summary within {args.timeout:g} s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
