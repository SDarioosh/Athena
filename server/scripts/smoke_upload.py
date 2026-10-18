#!/usr/bin/env python3
"""Smoke test against a running server: split audio files into 30 s Opus chunks and upload.

Usage:
    python scripts/smoke_upload.py --url http://127.0.0.1:8000 --token "$CONVO_INGEST_TOKEN" \
        --device trex3pro a.wav --silence 240 b.ogg

Arguments are processed in order: audio files are appended, ``--silence N`` inserts N
seconds of silence. Timestamps start at ``--start`` (default: now - total length) so the
chunks look like a real recording. Only uses stdlib + ffmpeg + httpx.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid

import httpx

SR = 16_000
CHUNK = 30


def load(path: str, max_seconds: float | None) -> bytes:
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def to_opus(pcm: bytes) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1",
         "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=pcm, capture_output=True, check=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--device", default="smoke")
    ap.add_argument("--session", default=None)
    ap.add_argument("--start", type=float, default=None, help="epoch seconds of first chunk")
    ap.add_argument("--max-seconds", type=float, default=None, help="trim each file")
    ap.add_argument("--no-end", action="store_true", help="don't call /end afterwards")
    args, rest = ap.parse_known_args()

    pcm = b""
    it = iter(rest)
    for item in it:
        if item == "--silence":
            pcm += b"\x00\x00" * int(float(next(it)) * SR)
        else:
            pcm += load(item, args.max_seconds)
    step = CHUNK * SR * 2
    chunks = [pcm[i:i + step] for i in range(0, len(pcm), step)]
    start = args.start if args.start is not None else time.time() - len(chunks) * CHUNK
    session = args.session or f"smoke-{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120) as http:
        for seq, chunk in enumerate(chunks):
            r = http.post("/v1/ingest/chunk",
                          files={"audio": (f"{seq}.opus", to_opus(chunk), "audio/ogg")},
                          data={"device": args.device, "session_id": session,
                                "sequence": str(seq), "timestamp": str(start + seq * CHUNK),
                                "codec": "opus"})
            print(seq, r.status_code, r.json())
            r.raise_for_status()
            if r.json().get("stop"):
                print("server requested stop:", r.json().get("reason"))
                break
        if not args.no_end:
            print(http.post(f"/v1/sessions/{session}/end").json())
    print("session:", session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
