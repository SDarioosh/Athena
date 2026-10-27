#!/usr/bin/env python3
"""Runs athena-meeting-bot built against the sandbox stub SDK (meeting-bot/bot/sdkstub)
through each fake meeting scenario and checks its status lines, exit code and timing
against the launcher contract (server/convo_server/meetings.py, server/README.md "Bot
contract"). A tiny local HTTP server stands in for Athena's upload API. Fictional data.

usage: stub_scenarios.py <path to athena-meeting-bot>
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_LINE = re.compile(r"^STATE (starting|waiting_room|waiting_permission|recording|ended|"
                        r"failed)(?: ([a-z0-9_]{1,40}))?$")
JWT = "jwt-placeholder-7f3a"
UPLOAD = "upload-placeholder-91bc"
JOIN = "join-placeholder-c4d2"
PASSCODE = "passcode-placeholder-55e1"
PRIVATE_TEXT = "private note to Athena"

REQUESTS: list[dict] = []
SESSIONS: set[str] = set()  # sessions that got a chunk (the real server makes them so)
LOCK = threading.Lock()
SESSION_PATH = re.compile(r"^/v1/sessions/([^/]+)/(chat|end)$")


def form_fields(content_type: str, body: bytes) -> dict[str, str]:
    """The text fields of a multipart upload (the audio part left out)."""
    msg = BytesParser(policy=default_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    return {part.get_param("name", header="content-disposition"):
            (part.get_payload(decode=True) or b"").decode()
            for part in msg.iter_parts() if part.get_filename() is None}


class Api(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        entry = {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        status = 200
        if self.path == "/v1/ingest/chunk":
            entry["fields"] = form_fields(self.headers.get("Content-Type", ""), body)
        with LOCK:
            if "fields" in entry:
                SESSIONS.add(entry["fields"].get("session_id", ""))
            m = SESSION_PATH.match(self.path)
            if m and m.group(1) not in SESSIONS:
                status = 404  # as Athena's API: no chunk yet, no such session
            entry["status"] = status
            REQUESTS.append(entry)
        out = json.dumps({"ok": True, "stop": False} if status == 200
                         else {"detail": "session not found"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: object) -> None:
        pass


def run(bot: str, url: str, name: str, scenario: str, expect: list[str], code: int,
        obf: bool = False, term_after: str | None = None, extra: dict | None = None,
        args: list[str] | None = None, env_extra: dict | None = None) -> list[str]:
    """Returns a list of problems (empty = passed)."""
    work = tempfile.mkdtemp(prefix=f"athena-bot-{name}-")
    opts = {"--meeting-id": "81234567890", "--session-id": f"zoom-{name}", "--upload-url": url,
            "--name": "Athena", "--max-seconds": "60", "--alone-seconds": "2",
            "--waiting-room-seconds": "2", "--codec": "opus", "--work-dir": work}
    opts.update(extra or {})
    argv = [bot] + (args if args is not None else [x for kv in opts.items() for x in kv])
    env = {"PATH": os.environ.get("PATH", ""), "HOME": work, "ATHENA_SDKSTUB_SCENARIO": scenario,
           "ATHENA_MEETING_JWT": JWT, "ATHENA_UPLOAD_TOKEN": UPLOAD,
           "ATHENA_MEETING_PASSCODE": PASSCODE}
    if obf:
        env.update({"ATHENA_ZOOM_JOIN_TOKEN": JOIN, "ATHENA_ZOOM_JOIN_TOKEN_KIND": "obf"})
    env.update(env_extra or {})
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, cwd=work, text=True)
    lines: list[str] = []
    problems: list[str] = []
    term_at = None
    stderr_chunks: list[str] = []
    t = threading.Thread(target=lambda: stderr_chunks.append(p.stderr.read()), daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    for raw in p.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        if term_after and line == f"STATE {term_after}" and term_at is None:
            time.sleep(0.5)
            term_at = time.monotonic()
            p.send_signal(signal.SIGTERM)
        if time.monotonic() > deadline:
            p.kill()
            problems.append("timed out")
            break
    rc = p.wait(timeout=30)
    t.join(timeout=5)
    shutil.rmtree(work, ignore_errors=True)
    if term_at is not None and time.monotonic() - term_at > 5.0:
        problems.append(f"exit took {time.monotonic() - term_at:.1f} s after SIGTERM (> 5 s)")
    for line in lines:
        if not STATE_LINE.match(line):
            problems.append(f"stdout line not a status line: {line!r}")
    got = [line.removeprefix("STATE ") for line in lines]
    if got != expect:
        problems.append(f"status lines {got} != {expect}")
    if rc != code:
        problems.append(f"exit code {rc} != {code}")
    output = "\n".join(lines) + "".join(stderr_chunks)
    for secret in (JWT, UPLOAD, JOIN, PASSCODE):
        if secret in output:
            problems.append("a secret was printed")
    return problems


DANA, LEE, SELF = "16779264", "16780288", "16778240"
SELF_AFTER_REJOIN = str(16778240 + 7)  # the stub's new id for the bot after a rejoin


def session_requests(name: str) -> list[dict]:
    sid = f"zoom-{name}"
    return [r for r in REQUESTS if r.get("fields", {}).get("session_id") == sid
            or r["path"] in (f"/v1/sessions/{sid}/chat", f"/v1/sessions/{sid}/end")]


def check_session(name: str, *, dana_chunks: int = 1, chat_lines: list[str] | None = None,
                  participants: list[str] | None = None, lee_left: bool = False,
                  breakouts: int = 0, silent_start: bool = False) -> list[str]:
    """What one scenario sent: chunks on Dana's track with sequences 0..n-1 (none twice,
    no restart after a rejoin, none of the bot's own ids), then the chat call (after a
    chunk, so the session exists) and /end, each once and answered 200."""
    mine = session_requests(name)
    sid = f"zoom-{name}"
    chunks = [r for r in mine if r["path"] == "/v1/ingest/chunk"]
    chat = [r for r in mine if r["path"] == f"/v1/sessions/{sid}/chat"]
    end = [r for r in mine if r["path"] == f"/v1/sessions/{sid}/end"]
    problems = []
    tracked = [r for r in chunks if r["fields"].get("track")]
    if silent_start:
        untracked = [r for r in chunks if not r["fields"].get("track")]
        if tracked or len(untracked) != 1 or untracked[0]["fields"].get("sequence") != "0":
            problems.append(f"want one silent chunk without a track, got {len(chunks)} chunk(s)")
    else:
        if any(r["fields"].get("track") != DANA for r in chunks):
            problems.append("a chunk on another track than Dana's: "
                            f"{sorted({r['fields'].get('track') for r in chunks})}")
        seqs = sorted(int(r["fields"]["sequence"]) for r in tracked)
        if seqs != list(range(len(seqs))) or len(seqs) < dana_chunks:
            problems.append(f"Dana's sequences {seqs} (want 0..n-1, n >= {dana_chunks})")
        if any(r["fields"].get("track_name") != "Dana Example" for r in tracked):
            problems.append("a chunk without Dana's name")
    if any(r["fields"].get("track") in (SELF, SELF_AFTER_REJOIN) for r in chunks):
        problems.append("the bot's own audio was uploaded")
    if any(r["fields"].get("device") != "zoom" for r in chunks):
        problems.append("a chunk not from device zoom")
    if len(end) != 1 or end[0]["status"] != 200:
        problems.append(f"end calls {[r['status'] for r in end]} (want one, 200)")
    if chat_lines is None:
        if chat:
            problems.append("an unexpected chat call")
    elif len(chat) != 1 or chat[0]["status"] != 200:
        problems.append(f"chat calls {[r['status'] for r in chat]} (want one, 200)")
    else:
        if REQUESTS.index(chat[0]) < max(REQUESTS.index(r) for r in chunks or [chat[0]]):
            problems.append("chat call before the last chunk")
        if end and REQUESTS.index(end[0]) < REQUESTS.index(chat[0]):
            problems.append("end call before the chat call")
        body = json.loads(chat[0]["body"])
        if body.get("topic") != "Q4 budget review (fictional)":
            problems.append("chat call without the topic")
        if [line.get("from") for line in body.get("lines", [])] != chat_lines:
            problems.append(f"chat lines {body.get('lines')}")
        names = sorted(p.get("name") for p in body.get("participants", []))
        if participants is not None and names != sorted(participants):
            problems.append(f"participants {names}")
        if "Athena" in names:
            problems.append("the bot listed as a participant")
        lee = [p for p in body.get("participants", []) if p.get("name") == "Lee Sample"]
        if lee_left and (not lee or not lee[0].get("left_at")):
            problems.append("Lee's leave time missing")
        got = body.get("breakouts", [])
        if len(got) != breakouts or any(not b.get("from") or not b.get("until") for b in got):
            problems.append(f"breakouts {got} (want {breakouts}, closed)")
    return problems


def check_sessions() -> int:
    both = ["Dana Example", "Lee Sample"]
    checks = {
        "ended": dict(chat_lines=["Dana Example"], participants=both, lee_left=True),
        "removed": dict(chat_lines=["Dana Example"], participants=both, lee_left=True),
        "droprejoin": dict(dana_chunks=2, chat_lines=["Dana Example"], participants=both),
        "sdkreconnect": dict(dana_chunks=2, chat_lines=["Dana Example"], participants=both),
        "droplost": dict(chat_lines=["Dana Example"], participants=both),
        "readmit": dict(dana_chunks=2, chat_lines=["Dana Example"], participants=both),
        "waitback": dict(chat_lines=["Dana Example"], participants=both),
        "breakout": dict(chat_lines=["Dana Example"], breakouts=1,
                         participants=both + ["Kim Placeholder", "Sam Fictional"]),
        "chatnoaudio": dict(chat_lines=["Dana Example"], participants=both,
                            silent_start=True),
    }
    failures = 0
    for name, kw in checks.items():
        problems = check_session(name, **kw)
        print(f"ok   {name} uploads" if not problems
              else f"FAIL {name} uploads: " + "; ".join(problems))
        failures += bool(problems)
    return failures


def main() -> int:
    bot = sys.argv[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    fast = {"ATHENA_SDKSTUB_FAST_REJOIN": "1"}
    cases = [
        ("sigterm", "record", ["recording", "ended stopped"], 0, False, "recording", None),
        ("ended", "ended", ["recording", "ended meeting_ended"], 0, False, None, None),
        ("removed", "removed", ["recording", "ended removed"], 0, False, None, None),
        ("permission", "permission", ["waiting_permission", "recording", "ended stopped"], 0,
         False, "recording", None),
        ("denied", "denied", ["waiting_permission", "waiting_permission denied",
                              "ended stopped"], 0, False, "waiting_permission denied", None),
        ("alone", "alone", ["recording", "ended alone"], 0, False, None, None),
        ("waitroom", "waiting_room", ["waiting_room", "failed waiting_room_timeout"], 1, False,
         None, None),
        ("waitroom_term", "waiting_room", ["waiting_room", "ended stopped"], 0, False,
         "waiting_room", {"--waiting-room-seconds": "60"}),
        ("nohost", "no_host", ["starting waiting_for_host", "failed meeting_not_started"], 1,
         False, None, None),
        ("passcode", "wrong_passcode", ["failed wrong_passcode"], 1, False, None, None),
        ("ownerlate", "owner_late", ["starting waiting_for_owner", "recording", "ended stopped"],
         0, True, "recording", {"--waiting-room-seconds": "30"}),
        ("noowner", "no_owner", ["starting waiting_for_owner", "failed owner_not_joined"], 1,
         True, None, {"--waiting-room-seconds": "4"}),
        ("ownerleft", "owner_left", ["recording", "ended owner_left"], 0, True, None, None),
        ("auth", "auth_fail", ["failed auth_failed"], 1, False, None, None),
        ("maxdur", "record", ["recording", "ended max_duration"], 0, False, None,
         {"--max-seconds": "3"}),
        # lane 5B
        ("droprejoin", "drop_rejoin", ["recording", "starting reconnecting",
                                       "waiting_permission", "recording",
                                       "ended meeting_ended"], 0, False, None, None),
        ("droplost", "drop_lost", ["recording", "starting reconnecting",
                                   "ended connection_lost"], 0, False, None, None, fast),
        ("dropterm", "drop_lost", ["recording", "starting reconnecting", "ended stopped"], 0,
         False, "starting reconnecting", None),
        ("obfdrop", "obf_drop", ["recording", "starting reconnecting", "ended owner_left"], 0,
         True, None, None),
        ("sdkreconnect", "sdk_reconnect", ["recording", "starting reconnecting", "recording",
                                           "ended meeting_ended"], 0, False, None, None),
        ("breakout", "breakout", ["recording", "ended meeting_ended"], 0, False, None, None),
        ("waitback", "waiting_room_back", ["recording", "waiting_room",
                                           "ended waiting_room_timeout"], 0, False, None, None),
        ("readmit", "waiting_room_readmit", ["recording", "waiting_room", "recording",
                                             "ended meeting_ended"], 0, False, None, None),
        ("chatnoaudio", "chat_no_audio", ["waiting_permission", "ended meeting_ended"], 0,
         False, None, None),
    ]
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(run, bot, url, c[0], c[1], c[2], c[3], c[4], c[5], c[6],
                            env_extra=c[7] if len(c) > 7 else None): c[0]
                for c in cases}
        futs[pool.submit(run, bot, url, "badargs", "record", ["failed bad_arguments"], 2,
                         args=["--meeting-id", "123"])] = "badargs"
        for fut in concurrent.futures.as_completed(futs):
            problems = fut.result()
            print(f"{'ok  ' if not problems else 'FAIL'} {futs[fut]}"
                  + ("" if not problems else ": " + "; ".join(problems)))
            failures += bool(problems)
    server.shutdown()
    for r in REQUESTS:
        if r["auth"] != f"Bearer {UPLOAD}":
            print(f"FAIL request {r['path']} without the upload token")
            failures += 1
        if PRIVATE_TEXT.encode() in r["body"]:
            print(f"FAIL a private chat message reached the server ({r['path']})")
            failures += 1
    failures += check_sessions()
    print(f"{len(REQUESTS)} request(s) to the fake upload API")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
