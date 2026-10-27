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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_LINE = re.compile(r"^STATE (starting|waiting_room|waiting_permission|recording|ended|"
                        r"failed)(?: ([a-z0-9_]{1,40}))?$")
JWT = "jwt-placeholder-7f3a"
UPLOAD = "upload-placeholder-91bc"
JOIN = "join-placeholder-c4d2"
PASSCODE = "passcode-placeholder-55e1"
PRIVATE_TEXT = "private note to Athena"

REQUESTS: list[dict] = []
LOCK = threading.Lock()


class Api(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with LOCK:
            REQUESTS.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": body})
        out = json.dumps({"ok": True, "stop": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: object) -> None:
        pass


def run(bot: str, url: str, name: str, scenario: str, expect: list[str], code: int,
        obf: bool = False, term_after: str | None = None, extra: dict | None = None,
        args: list[str] | None = None) -> list[str]:
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


def main() -> int:
    bot = sys.argv[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
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
    ]
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(run, bot, url, n, sc, ex, code, obf, term, extra): n
                for n, sc, ex, code, obf, term, extra in cases}
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
    print(f"{len(REQUESTS)} request(s) to the fake upload API")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
