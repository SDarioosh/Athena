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
TODOS = "/api/codex/todos"


def demo_notes() -> list[dict[str, Any]]:
    """Fictional notes in the hub's order: pinned first, then the most recently updated."""
    return [
        {"id": "n-groceries", "title": "Groceries", "type": "checklist", "label": "home",
         "pinned": True, "due_date": "2026-10-21T00:00:00", "updated_at": "2026-10-20T08:00:00Z",
         "items": [{"text": "Milk", "done": False}, {"text": "Bread", "done": True},
                   {"text": "Eggs  (free range)", "done": False}], "content": None},
        {"id": "n-idea", "title": "Idea", "type": "note", "label": "", "pinned": False,
         "due_date": None, "updated_at": "2026-10-19T10:00:00Z", "items": [],
         "content": "Plain text, not a checklist"},
        {"id": "n-trip", "title": "", "type": "checklist", "label": "", "pinned": False,
         "due_date": None, "updated_at": "2026-10-18T10:00:00Z",
         "items": [{"text": "Passport", "done": False}, {"text": "Charger", "done": False}],
         "content": None},
        {"id": "n-empty", "title": "Empty list", "type": "checklist", "label": "",
         "pinned": False, "due_date": None, "updated_at": "2026-10-17T10:00:00Z",
         "items": [], "content": None},
    ]


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
        self.notes = demo_notes()
        self.todos_mode = "json"  # "json", "text" (an older hub) or "forbidden" (403)
        self.toggle_exit_code = 0
        self._n = 2
        ody = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _handle(self, method: str) -> None:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else None
                status, answer = ody.route(method, self.path, dict(self.headers.items()), body)
                if isinstance(answer, str):
                    raw, ctype = answer.encode(), "text/plain; charset=utf-8"
                else:
                    raw, ctype = json.dumps(answer).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
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
              body: Any) -> tuple[int, Any]:
        self.requests.append((method, raw_path, headers, body))
        if headers.get("Authorization") != f"Bearer {ODY_TOKEN}":
            return 401, {"detail": "unauthorized"}
        if self.fail:
            return self.fail.pop(0), {"detail": "injected"}
        url = urllib.parse.urlsplit(raw_path)
        path = url.path
        if path == TODOS:
            return self._todos(method, dict(urllib.parse.parse_qsl(url.query)), body)
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

    def _todos(self, method: str, query: dict[str, str], body: Any) -> tuple[int, Any]:
        if self.todos_mode == "forbidden":
            return 403, {"detail": "token lacks scope todos:read"}
        if method == "GET":
            if self.todos_mode == "text" or query.get("format") != "json":
                return 200, "You have 2 checklists: Groceries (Milk, Eggs), ..."
            return 200, {"notes": self.notes}
        if method == "POST" and body.get("action") == "toggle_item":
            note = next((n for n in self.notes if n["id"] == body.get("note_id")), None)
            index = body.get("index")
            if self.toggle_exit_code or note is None or not 0 <= index < len(note["items"]):
                return 200, {"exit_code": 1, "error": "no such item"}
            note["items"][index]["done"] = bool(body["done"])
            return 200, {"exit_code": 0, "output": "ok"}
        return 400, {"detail": "bad todos request"}

    def bodies(self, path: str = EVENTS) -> list[Any]:
        return [b for m, p, _, b in self.requests if m == "POST" and p.startswith(path)]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
