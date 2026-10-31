#!/usr/bin/env python3
"""Fake Athena ingest API for the core's host tests (stdlib only, loopback, fictional data).

    fake_ingest_server.py --mode ok|strict|503xN|503always|401|stop|hang|400first|case
                          --log DIR [--token test-token] [--case JSON]

strict: as Athena's API, /chat and /end answer 404 for a session no chunk made yet.
case: the first chunk call gets --case, one row of docs/api/ingest-client-cases.json
({"status", "body"}; status 0 closes the connection unanswered, a string body is sent as
text, a 3xx carries a Location header); every later call is answered as in "ok".
Prints the port on its first stdout line. Every request is appended to DIR/requests.jsonl
(chunk audio saved next to it); the routes and answers mimic server/athena/api.py.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import threading
import time
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCK = threading.Lock()
STATE = {"chunk_calls": 0, "n": 0, "sessions": set()}


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict]]:
    msg = BytesParser(policy=default_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields: dict[str, str] = {}
    files: list[dict] = []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files.append({"name": name, "filename": part.get_filename(),
                          "type": part.get_content_type(), "data": data})
        else:
            try:  # as Starlette's form parser: UTF-8, else Latin-1
                fields[name] = data.decode("utf-8")
            except UnicodeDecodeError:
                fields[name] = data.decode("latin-1")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def answer(self, status: int, obj: dict | str | None) -> None:
        if status == 0:  # a network error: no answer at all
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if isinstance(obj, str) or obj is None:
            data, ctype = (obj or "").encode(), "text/html"
        else:
            data, ctype = json.dumps(obj).encode(), "application/json"
        self.send_response(status)
        if 300 <= status < 400:
            self.send_header("Location", "http://127.0.0.1:9/v1/ingest/chunk")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        cfg = self.server.cfg
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        entry: dict = {"path": self.path, "auth": auth, "t": time.time(),
                       "content_type": self.headers.get("Content-Type", "")}
        with LOCK:
            STATE["n"] += 1
            n = STATE["n"]
        mode = cfg.mode
        if self.path == "/v1/ingest/chunk":
            fields, files = parse_multipart(entry["content_type"], body)
            entry["fields"] = fields
            entry["files"] = []
            for f in files:
                p = Path(cfg.log) / f"req{n:05d}-{f['filename']}"
                p.write_bytes(f["data"])
                entry["files"].append({"name": f["name"], "filename": f["filename"],
                                       "type": f["type"], "path": str(p)})
            with LOCK:
                STATE["chunk_calls"] += 1
                calls = STATE["chunk_calls"]
                STATE["sessions"].add(fields.get("session_id"))
        else:
            entry["body"] = body.decode("utf-8", "replace")
            calls = 0
        status, obj = self.decide(mode, calls, auth)
        entry["status"] = status
        with LOCK, open(Path(cfg.log) / "requests.jsonl", "a") as out:
            out.write(json.dumps(entry) + "\n")
        if status == -1:  # hang: never answer
            time.sleep(3600)
            return
        self.answer(status, obj)

    def decide(self, mode: str, calls: int, auth: str) -> tuple[int, dict | str | None]:
        if auth != f"Bearer {self.server.cfg.token}":
            return 401, {"detail": "invalid or missing bearer token"}
        if mode == "hang":
            return -1, {}
        if mode == "case" and self.path == "/v1/ingest/chunk" and calls == 1:
            case = json.loads(self.server.cfg.case)
            return case["status"], case["body"]
        m = re.fullmatch(r"/v1/sessions/([^/]+)/(end|chat)", self.path)
        if m:
            if mode == "strict" and m.group(1) not in STATE["sessions"]:
                return 404, {"detail": "session not found"}
            if m.group(2) == "end":
                return 200, {"ok": True, "stop": True}
            return 200, {"ok": True, "session_id": m.group(1), "result": "queued"}
        if self.path != "/v1/ingest/chunk":
            return 404, {"detail": "Not Found"}
        if mode == "401":
            return 401, {"detail": "invalid or missing bearer token"}
        if mode == "503always":
            return 503, {"detail": "busy"}
        if mode.startswith("503x") and calls <= int(mode[4:]):
            return 503, {"detail": "busy"}
        if mode == "400first" and calls == 1:
            return 400, {"detail": "bad chunk"}
        stop = mode == "stop"
        return 200, {"ok": True, "stop": stop, "reason": "max_duration" if stop else None,
                     "duplicate": False}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--log", required=True)
    ap.add_argument("--token", default="test-token")
    ap.add_argument("--case", default="{}")
    cfg = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.cfg = cfg
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
