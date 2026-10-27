#!/usr/bin/env python3
"""End-to-end host tests of athena_core against fake_ingest_server.py (loopback only).

Run by ctest: test_core_upload.py --driver <core_driver> --server <fake_ingest_server.py>
Checks the upload fields, per-track sequences, the queue, retries, 401, "stop", the chat
call body (server/README.md "Meeting chat, topic and participants") and finish()'s
deadline. Fictional data only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ARGS: argparse.Namespace
SESSION = "mtg-demo-1"
TOKEN = "test-token"
DANA, LEE = "16782336", "16782337"
TRACK_RE = r"[A-Za-z0-9._:-]{1,64}"


def opus_seconds(data: bytes) -> float:
    """Duration of an Ogg/Opus file from its last granule position and pre-skip."""
    assert data[:4] == b"OggS", "not an Ogg file"
    head = data.find(b"OpusHead")
    assert head >= 0, "no OpusHead"
    preskip = struct.unpack_from("<H", data, head + 10)[0]
    rate = struct.unpack_from("<I", data, head + 12)[0]
    assert rate == 32000, rate
    granule, at = -1, data.find(b"OggS")
    while at >= 0:
        g = struct.unpack_from("<q", data, at + 6)[0]
        if g >= 0:
            granule = g
        at = data.find(b"OggS", at + 4)
    return (granule - preskip) / 48000.0


def wav_seconds(data: bytes) -> float:
    assert data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt ", "not a WAV file"
    channels, rate = struct.unpack_from("<HI", data, 22)
    assert channels == 1 and rate == 32000, (channels, rate)
    size = struct.unpack_from("<I", data, 40)[0]
    assert size == len(data) - 44
    return size / 2 / rate


def iso_ok(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class Run:
    """One fake server + one core_driver run."""

    def __init__(self, mode: str, *driver_args: str, timeout: float = 120):
        self.tmp = Path(tempfile.mkdtemp(prefix="athena-core-test-"))
        self.log = self.tmp / "log"
        self.work = self.tmp / "work"
        self.log.mkdir()
        self.work.mkdir()
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        server = subprocess.Popen([sys.executable, ARGS.server, "--mode", mode, "--log",
                                   str(self.log), "--token", TOKEN],
                                  stdout=subprocess.PIPE, text=True, env=env)
        try:
            port = int(server.stdout.readline())
            out = subprocess.run(
                [ARGS.driver, "--url", f"http://127.0.0.1:{port}", "--session", SESSION,
                 "--token", TOKEN, "--work", str(self.work), *driver_args],
                capture_output=True, text=True, timeout=timeout, check=True)
        finally:
            server.kill()
            server.wait()
            server.stdout.close()
        self.stderr = out.stderr
        self.result = dict(line.split("=", 1) for line in out.stdout.split())
        path = self.log / "requests.jsonl"
        self.requests = [json.loads(x) for x in path.read_text().splitlines()] \
            if path.exists() else []

    def num(self, key: str) -> int:
        return int(self.result[key])

    def chunks(self, status: int | None = 200) -> list[dict]:
        return [r for r in self.requests if r["path"] == "/v1/ingest/chunk"
                and (status is None or r["status"] == status)]

    def calls(self, suffix: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == f"/v1/sessions/{SESSION}/{suffix}"]

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class CoreUploadTests(unittest.TestCase):
    def run_core(self, mode: str, *args: str, **kw) -> Run:
        run = Run(mode, *args, **kw)
        self.addCleanup(run.cleanup)
        return run

    def check_meeting(self, run: Run, codec: str) -> None:
        chunks = run.chunks()
        by_track: dict[str, list[dict]] = {}
        for c in chunks:
            f = c["fields"]
            self.assertEqual(set(f), {"device", "session_id", "sequence", "timestamp",
                                      "codec", "track", "track_name"})
            self.assertEqual(f["device"], "zoom")
            self.assertEqual(f["session_id"], SESSION)
            self.assertEqual(f["codec"], codec)
            self.assertRegex(f["track"], TRACK_RE)
            self.assertEqual(c["auth"], f"Bearer {TOKEN}")
            self.assertEqual(len(c["files"]), 1)
            self.assertEqual(c["files"][0]["name"], "audio")
            float(f["timestamp"])  # epoch seconds
            by_track.setdefault(f["track"], []).append(c)
        self.assertEqual(set(by_track), {DANA, LEE})
        expect = {DANA: [(0, 30.0), (30, 30.0), (60, 10.0)], LEE: [(5, 10.0), (40, 5.0)]}
        t0 = 1760367600.0
        for track, items in by_track.items():
            items.sort(key=lambda c: int(c["fields"]["sequence"]))
            self.assertEqual([int(c["fields"]["sequence"]) for c in items],
                             list(range(len(expect[track]))), track)
            for c, (start, seconds) in zip(items, expect[track], strict=True):
                self.assertAlmostEqual(float(c["fields"]["timestamp"]), t0 + start, places=3)
                data = Path(c["files"][0]["path"]).read_bytes()
                dur = opus_seconds(data) if codec == "opus" else wav_seconds(data)
                self.assertAlmostEqual(dur, seconds, delta=0.002)
                mime = "audio/ogg" if codec == "opus" else "audio/wav"
                self.assertEqual(c["files"][0]["type"], mime)
        names = {int(c["fields"]["sequence"]): c["fields"]["track_name"] for c in by_track[DANA]}
        self.assertEqual(names[0], "Dana Example")
        self.assertEqual(names[2], "Dana E.")  # renamed during chunk 2

    def check_chat_body(self, body: dict, lines: int) -> None:
        """The README contract of POST /v1/sessions/{id}/chat."""
        self.assertEqual(set(body), {"lines", "topic", "participants"})
        self.assertEqual(body["topic"], "Q4 budget review")
        self.assertEqual(len(body["lines"]), lines)
        for line in body["lines"]:
            self.assertLessEqual(set(line), {"at", "from", "zoom_user_id", "text", "private"})
            self.assertTrue(iso_ok(line["at"]), line["at"])
            self.assertIsInstance(line["text"], str)
            self.assertTrue(line["text"])
            self.assertLessEqual(len(line["text"]), 4000)
            self.assertIs(line["private"], False)
            self.assertNotIn("PRIVATE", line["text"])
            self.assertRegex(line["zoom_user_id"], TRACK_RE)
        first = body["lines"][0]
        self.assertEqual(first, {"at": "2025-10-13T15:00:21.000Z", "from": "Dana Example",
                                 "zoom_user_id": DANA,
                                 "text": "Here's the sheet: https://example.invalid/budget-v3.xlsx",
                                 "private": False})
        self.assertEqual(body["lines"][1]["text"], "Thanks, looks good")
        parts = {p["zoom_user_id"]: p for p in body["participants"]}
        self.assertEqual(parts[DANA], {"zoom_user_id": DANA, "name": "Dana E.",
                                       "joined_at": "2025-10-13T15:00:02.000Z",
                                       "left_at": None})
        self.assertEqual(parts[LEE]["joined_at"], "2025-10-13T15:00:03.000Z")
        self.assertIsNone(parts[LEE]["left_at"])  # back at 40 s

    def test_meeting_opus(self):
        run = self.run_core("ok")
        self.assertEqual(run.num("finish"), 1, run.stderr)
        self.assertEqual(run.num("stop"), 0)
        self.check_meeting(run, "opus")
        self.assertEqual(len(run.chunks(None)), 5)  # no retries, no duplicates
        chat, end = run.calls("chat"), run.calls("end")
        self.assertEqual((len(chat), len(end)), (1, 1))
        # order: every chunk, then the chat call, then /end
        paths = [r["path"] for r in run.requests]
        self.assertEqual(paths[-2:], [chat[0]["path"], end[0]["path"]])
        self.assertEqual(chat[0]["content_type"], "application/json")
        self.assertEqual(chat[0]["auth"], f"Bearer {TOKEN}")
        self.check_chat_body(json.loads(chat[0]["body"]), 2)
        # nothing left behind after a successful finish()
        self.assertEqual(sorted(os.listdir(run.work)), [])

    def test_meeting_wav(self):
        run = self.run_core("ok", "--codec", "wav")
        self.assertEqual(run.num("finish"), 1, run.stderr)
        self.check_meeting(run, "wav")

    def test_queue_survives_503_then_200(self):
        run = self.run_core("503x2", "--deadline-ms", "20000")
        self.assertGreaterEqual(run.num("queue_files_before_finish"), 1)
        self.assertEqual(run.num("finish"), 1, run.stderr)
        self.assertEqual(len(run.chunks(503)), 2)
        self.check_meeting(run, "opus")
        # the retried chunk is the same chunk, sent again
        failed = run.chunks(503)
        self.assertEqual(failed[0]["fields"], failed[1]["fields"])
        self.assertEqual(sorted(os.listdir(run.work)), [])

    def test_401_sets_upload_rejected(self):
        run = self.run_core("401", "--wait-ms", "5000")
        self.assertEqual(run.num("rejected"), 1)
        self.assertEqual(run.num("finish"), 0)
        self.assertEqual(len(run.chunks(None)), 1)  # gives up at once, no retry loop
        self.assertEqual(run.calls("chat") + run.calls("end"), [])
        self.assertLess(run.num("elapsed_ms"), 1000)

    def test_stop_true_sets_stop_requested(self):
        run = self.run_core("stop", "--wait-ms", "5000")
        self.assertEqual(run.num("stop"), 1)
        self.assertEqual(run.num("rejected"), 0)
        self.assertEqual(run.num("finish"), 1, run.stderr)

    def test_finish_honours_deadline_with_silent_server(self):
        run = self.run_core("hang", "--deadline-ms", "2000")
        self.assertEqual(run.num("finish"), 0)
        self.assertLessEqual(run.num("elapsed_ms"), 2300)
        self.assertGreaterEqual(run.num("elapsed_ms"), 1000)  # it did try to send

    def test_refused_chunk_is_dropped(self):
        run = self.run_core("400first")
        self.assertEqual(run.num("finish"), 0)  # not everything was sent
        self.assertEqual(len(run.chunks(400)), 1)
        self.assertEqual(len(run.chunks(200)), 4)  # the rest went, no retry of the 400
        self.assertEqual(len(run.calls("chat")), 1)
        self.assertEqual(len(run.calls("end")), 1)

    def test_queue_cap_drops_oldest(self):
        run = self.run_core("503always", "--audio-seconds", "150", "--max-seconds", "60",
                            "--deadline-ms", "1500")
        self.assertEqual(run.num("finish"), 0)
        # 150 s of Dana (5 chunks) + Lee (2): at most 60 s queued, plus one in flight
        self.assertLessEqual(run.num("queue_files_before_finish"), 4)
        self.assertGreaterEqual(run.num("queue_files_before_finish"), 2)
        self.assertIn("queue over the cap", run.stderr)

    def test_chat_cap_and_private_drop(self):
        run = self.run_core("ok", "--chat-flood")
        self.assertEqual(run.num("chat_lines_before_finish"), 2000)
        self.assertEqual(run.num("finish"), 1, run.stderr)
        body = json.loads(run.calls("chat")[0]["body"])
        self.check_chat_body(body, 2000)
        self.assertEqual(body["lines"][-1]["text"], "flood 1997")


def main() -> None:
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", required=True)
    ap.add_argument("--server", required=True)
    ARGS, rest = ap.parse_known_args()
    unittest.main(argv=[sys.argv[0], "-v", *rest])


if __name__ == "__main__":
    main()
