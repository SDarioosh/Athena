"""A fake Odysseus: the calendar's token routes (/api/codex/calendar/events) over HTTP, with
the shapes of docs/t5-pda-plan.md §6. Fictional data only."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ODY_TOKEN = "ody_fictional-calendar-token-7010"
EVENTS = "/api/codex/calendar/events"


class FakeOdysseus:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {
            "u1": {"uid": "u1", "summary": "Dentist follow-up",
                   "dtstart": "2026-10-21T09:00:00Z", "dtend": "2026-10-21T09:30:00Z",
                   "all_day": False, "location": "Dr. Patel", "calendar": "Home",
                   "calendar_href": "cal-home"},
            "u2::20261022": {"uid": "u2::20261022", "summary": "Run",
                             "dtstart": "2026-10-22", "dtend": "2026-10-23", "all_day": True,
                             "location": ""},
        }
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.fail: list[int] = []
        self._n = 2
        ody = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _handle(self, method: str) -> None:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else None
                status, answer = ody.route(method, self.path, dict(self.headers.items()), body)
                raw = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def route(self, method: str, raw_path: str, headers: dict[str, str],
              body: Any) -> tuple[int, dict]:
        self.requests.append((method, raw_path, headers, body))
        if headers.get("Authorization") != f"Bearer {ODY_TOKEN}":
            return 401, {"detail": "unauthorized"}
        if self.fail:
            return self.fail.pop(0), {"detail": "injected"}
        path = urllib.parse.urlsplit(raw_path).path
        if method == "GET" and path == EVENTS:
            return 200, {"events": sorted(self.events.values(), key=lambda e: e["dtstart"])}
        if method == "POST" and path == EVENTS:
            if not body.get("summary") or not body.get("dtstart"):
                return 400, {"detail": "invalid"}
            self._n += 1
            uid = f"u{self._n}"
            self.events[uid] = {"uid": uid, "location": "", **body}
            return 200, {"ok": True, "uid": uid, "event": self.events[uid]}
        if method == "DELETE" and path.startswith(EVENTS + "/"):
            uid = urllib.parse.unquote(path[len(EVENTS) + 1:])
            if self.events.pop(uid, None) is None:
                return 404, {"detail": "event not found"}
            return 200, {"ok": True}
        return 404, {"detail": "not found"}

    def bodies(self) -> list[Any]:
        return [b for m, _, _, b in self.requests if m == "POST"]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
