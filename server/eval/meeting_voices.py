#!/usr/bin/env python3
"""Meeting voices eval (Zoom bot lane 2C): does a meeting teach Athena its participants'
voices well enough to name them in a later in-person recording?

Public AMI audio (CC-BY-4.0, see recognition.py; downloaded into eval/data/, never
committed) stands in for a Zoom meeting: AMI's ES2004 series is the same four people in
four sessions, each wearing their own headset.

1. A "past" in-person recording: ES2004b, table array mic (or the headset mix), window
   W1. Nobody is known yet, so every voice is "Speaker N".
2. A meeting: ES2004a, each person's own headset as their own track under a fictional
   name (recognition.NAMES), as the Zoom bot uploads it. Lane 2C learns up to 3 samples
   per person and relabels matching "Speaker N" of step 1.
3. A "new" in-person recording: ES2004b window W2 (right after W1), named live from the
   learned voices.

Reported per step: "named right" (share of each enrolled person's single-speaker frames
labelled with their name) and "named wrong" (share of all frames labelled with someone
else's name), as in recognition.py; plus the samples learned and the lines relabeled.
Needs whisper-server and the diarization extra (ECAPA); settings come from CONVO_* like
the server.

    python eval/recognition.py fetch --meetings ES2004a ES2004b --audio sim array
    python eval/meeting_voices.py --whisper-url http://127.0.0.1:8081 --audio array
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recognition as rec  # noqa: E402
from recognition import (  # noqa: E402
    ARRAY_FILE,
    CHUNK,
    DATA,
    HEADSET_FILE,
    MIX_FILE,
    NAMES,
    RESULTS,
    SAMPLE_RATE,
    Pipeline,
    Settings,
    Store,
    audio_mod,
    build_diarizer,
    build_transcriber,
    make_vad,
    meeting_speakers,
    rttm_turns,
)

T0 = 1_760_000_000.0
DAY = 86_400.0


def ingest_meeting(pipeline: Pipeline, meeting: str, start: float, end: float,
                   names: dict[str, str]) -> str:
    """Each AMI headset as one Zoom track (30 s chunks), under the speaker's fictional
    name; returns the session id."""
    sid = f"zoom-{meeting}"
    step = int(CHUNK * SAMPLE_RATE)
    for s in meeting_speakers(meeting):
        f = DATA / HEADSET_FILE.format(m=meeting, ch=s["channel"])
        if not f.exists():
            raise SystemExit(f"{f.name} missing: run `recognition.py fetch --audio sim`")
        pcm = audio_mod.decode(f.read_bytes(), "wav")[int(start * SAMPLE_RATE):
                                                      int(end * SAMPLE_RATE)]
        for i in range(0, pcm.size, step):
            pipeline.ingest_chunk(device="zoom", session_id=sid, sequence=i // step,
                                  started_at=T0 + 2 * DAY + i / SAMPLE_RATE, codec="wav",
                                  data=audio_mod.to_wav_bytes(pcm[i:i + step]),
                                  track=f"ch{s['channel']}", track_name=names[s["speaker"]])
    pipeline.end_session(sid)
    pipeline.process_session(sid)
    return sid


def naming(store: Store, sid: str, t0: float, turns, start: float, end: float,
           names: dict[str, str]) -> dict:
    by_name = {v: k for k, v in names.items()}
    segs = [(sg.started_at - t0, sg.ended_at - t0, by_name.get(sg.speaker, sg.speaker),
             sg.text) for sg in store.session_segments(sid)]
    out = rec._naming(turns, start, end, segs, list(names))
    return {"named_right": out.get("named_right"), "named_wrong": out.get("named_wrong"),
            "labels": sorted({s for _, _, s, _ in segs})}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meeting", default="ES2004a", help="the Zoom stand-in (headsets)")
    ap.add_argument("--recording", default="ES2004b", help="in person, same people")
    ap.add_argument("--audio", choices=("array", "headset"), default="array")
    ap.add_argument("--meeting-seconds", type=float, default=600.0)
    ap.add_argument("--offset", type=float, default=300.0, help="W1 start in --recording")
    ap.add_argument("--seconds", type=float, default=300.0, help="length of W1 and of W2")
    ap.add_argument("--whisper-url")
    ap.add_argument("--label", default="meeting-voices")
    args = ap.parse_args()

    people = meeting_speakers(args.meeting)
    names = {s["speaker"]: NAMES[i] for i, s in enumerate(people)}  # AMI id -> name
    rec_file = DATA / (ARRAY_FILE if args.audio == "array" else MIX_FILE).format(
        m=args.recording)
    full = audio_mod.decode(rec_file.read_bytes(), "wav")
    turns = rttm_turns(args.recording)
    w1 = (args.offset, args.offset + args.seconds)
    w2 = (w1[1], w1[1] + args.seconds)
    base = {"ingest_token": "eval-token-not-a-secret-000000", "vad_backend": "silero",
            "models_dir": DATA.parent / "models", "learn_meeting_voices": True}
    if args.whisper_url:
        base["whisper_server_url"] = args.whisper_url
    with tempfile.TemporaryDirectory(prefix="convo-eval-") as tmp:
        settings = Settings(**{**base, "data_dir": Path(tmp)})
        store = Store(settings.db_path)
        diarizer = build_diarizer(settings, store)
        if diarizer is None:
            raise SystemExit("diarization is off (CONVO_DIARIZATION=false)")
        pipeline = Pipeline(settings, store, make_vad(settings.vad_backend, settings),
                            build_transcriber(settings), diarizer)
        cut = lambda w: full[int(w[0] * SAMPLE_RATE): int(w[1] * SAMPLE_RATE)]  # noqa: E731
        print(f"1. past recording {args.recording} {w1} ({args.audio}) ...", flush=True)
        rec._process(pipeline, "past", cut(w1), T0)
        before = naming(store, "past", T0, turns, *w1, names)
        print(f"2. meeting {args.meeting} (headsets as tracks, "
              f"{args.meeting_seconds:.0f} s) ...", flush=True)
        ingest_meeting(pipeline, args.meeting, 0.0, args.meeting_seconds, names)
        learned = {r["name"]: r["n"] for r in store._q(
            "SELECT s.name, COUNT(*) AS n FROM voice_samples v JOIN speakers s"
            " ON s.id = v.speaker_id WHERE v.source = 'meeting' GROUP BY s.name")}
        relabeled = naming(store, "past", T0, turns, *w1, names)
        print(f"3. new recording {args.recording} {w2} ...", flush=True)
        rec._process(pipeline, "new", cut(w2), T0 + 3 * DAY)
        live = naming(store, "new", T0 + 3 * DAY, turns, *w2, names)
        store.close()
    out = {"meeting": args.meeting, "recording": args.recording, "audio": args.audio,
           "learned_samples": learned, "past_before": before, "past_relabeled": relabeled,
           "new_recording": live}
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{args.label}.json"
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
