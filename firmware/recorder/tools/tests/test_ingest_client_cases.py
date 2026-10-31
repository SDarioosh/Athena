"""The PC relay's uploader against every row of docs/api/ingest-client-cases.json.

Each row is served by a local HTTP server as the answer to one `upload_chunk`; the
uploader's outcome is mapped onto the contract's classes:

- `upload_chunk` returns          -> ok
- `UploadError` kind `RETRY`      -> retry
- `UploadError` kind `AUTH`       -> stop_uploads
- `UploadError` kind `SKIP`       -> drop (the relay leaves the chunk on the watch
  unacknowledged and skips it for this connection; docs/api/ingest-client.md)

Status 0 (no HTTP answer) is a server that is not listening. Fictional data only.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from athena_relay.uploader import AUTH, RETRY, SKIP, Uploader, UploadError

CASES_FILE = Path(__file__).resolve().parents[4] / "docs" / "api" / "ingest-client-cases.json"
CASES: list[dict[str, Any]] = json.loads(CASES_FILE.read_text())
KIND_TO_EXPECT = {RETRY: "retry", AUTH: "stop_uploads", SKIP: "drop"}
TOKEN = "fictional-cases-token-41c7"


def _row_id(row: dict[str, Any]) -> str:
    body = row["body"]
    tag = "null" if body is None else ("json" if isinstance(body, dict) else "text")
    return f"{row['status']}-{tag}-{row['expect']}"


def _serve(row: dict[str, Any]) -> ThreadingHTTPServer:
    status, body = row["status"], row["body"]
    if body is None:
        raw, ctype = b"", None
    elif isinstance(body, str):
        raw, ctype = body.encode(), "text/html"
    else:
        raw, ctype = json.dumps(body).encode(), "application/json"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            if 300 <= status < 400:
                # A redirect to another host: the uploader must not follow it.
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _outcome(uploader: Uploader) -> str:
    try:
        uploader.upload_chunk(device="twatch-ultra", session_id="3f9a0c11d2e4b507", sequence=2,
                              timestamp="2026-10-19T12:34:56.789Z", audio=b"\x00" * 64)
    except UploadError as e:
        return KIND_TO_EXPECT[e.kind]
    return "ok"


def test_cases_file_covers_every_class() -> None:
    assert {row["expect"] for row in CASES} == {"ok", "retry", "drop", "stop_uploads"}


@pytest.mark.parametrize("row", CASES, ids=_row_id)
def test_ingest_client_case(row: dict[str, Any]) -> None:
    if row["status"] == 0:
        uploader = Uploader(f"http://127.0.0.1:{_closed_port()}", TOKEN, timeout=2.0)
        assert _outcome(uploader) == row["expect"]
        return
    server = _serve(row)
    try:
        uploader = Uploader(f"http://127.0.0.1:{server.server_address[1]}", TOKEN, timeout=5.0)
        assert _outcome(uploader) == row["expect"]
    finally:
        server.shutdown()
        server.server_close()
