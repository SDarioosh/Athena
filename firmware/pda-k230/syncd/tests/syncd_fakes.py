"""Test fakes for syncd: the UI end of `glance.sock` (the PC relay's fake T5 behind a Unix
socket) and a fake Odysseus hub (its Athena relay routes over the fake Athena API, the
calendar/todos/insight routes of the fake Odysseus, and `/api/pair/redeem`). Fictional
data only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import _syncd_path  # noqa: F401 (the import path)
from athena_pda_sync import frames as fr
from athena_relay.drain import LinkLost
from ody_fake import ODY_TOKEN, FakeOdysseus
from pda_fakes import TOKEN, FakeApiServer, FakeT5

K230_INFO = b"proto=1;device=k230-pda;fw=0.1.0;board=t_display_k230"
HUB_TOKEN = "ody_fictional-k230-pda-token-0123456789"
CODE = "Fict1onalPairCode_k230"


def short_tmp() -> Path:
    """A short directory (a Unix socket path is at most 107 bytes)."""
    return Path(tempfile.mkdtemp(prefix="k230-"))


# --------------------------------------------------------------------- fake UI


class FakeUi:
    """The UI's side of glance.sock: CTRL goes to a FakeT5, its answers come back as DATA,
    its status notifications as STATUS. One client at a time (a second is refused)."""

    def __init__(self, path: Path, t5: FakeT5 | None = None) -> None:
        self.path = path
        self.t5 = t5 or FakeT5(info=K230_INFO)
        self.locals: list[dict[str, Any]] = []
        self.frames: list[int] = []  # types received
        self.connections = 0
        self.refused = 0
        self.writer: asyncio.StreamWriter | None = None
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self._client, str(self.path))
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        await self.drop()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def drop(self) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
            self.writer = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.writer is not None:
            self.refused += 1
            writer.close()
            return
        self.writer = writer
        self.connections += 1
        self.t5.reconnect()
        try:
            while True:
                kind, n = fr.decode_header(await reader.readexactly(fr.HEADER))
                payload = await reader.readexactly(n) if n else b""
                self.frames.append(kind)
                if kind == fr.CTRL:
                    try:
                        await self.t5.write(payload)
                    except LinkLost:
                        break
                elif kind == fr.STATUS_REQ:
                    self._send(fr.STATUS, self.t5.status().encode())
                elif kind == fr.INFO_REQ:
                    self._send(fr.INFO, self.t5.info)
                elif kind == fr.LOCAL:
                    self.locals.append(json.loads(payload))
                if not self._flush():
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None
            writer.close()

    def _send(self, kind: int, payload: bytes) -> None:
        if self.writer is not None:
            self.writer.write(fr.encode(kind, payload))

    def _flush(self) -> bool:
        """Send what the fake T5 queued. False if it wants the link dropped."""
        while not self.t5.queue.empty():
            item = self.t5.queue.get_nowait()
            if item is None:
                return False
            self._send(fr.DATA, item)
        while not self.t5.status_queue.empty():
            item = self.t5.status_queue.get_nowait()
            if item is not None:
                self._send(fr.STATUS, item)
        return True

    async def send_local(self, message: dict[str, Any]) -> None:
        assert self.writer is not None
        self.writer.write(fr.encode_local(message))
        await self.writer.drain()

    async def notify_status(self) -> None:
        self.t5.notify_status()
        self._flush()
        if self.writer is not None:
            await self.writer.drain()

    def of(self, t: str) -> list[dict[str, Any]]:
        return [m for m in self.locals if m.get("t") == t]

    async def wait_for(self, pred, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > end:
                raise AssertionError("timed out waiting")
            await asyncio.sleep(0.01)

    async def wait_local(self, t: str, n: int = 1, timeout: float = 10.0,
                         **fields: Any) -> dict[str, Any]:
        """The n-th LOCAL of type `t` (whose fields match `fields`)."""
        def hits() -> list[dict[str, Any]]:
            return [m for m in self.of(t) if all(m.get(k) == v for k, v in fields.items())]

        await self.wait_for(lambda: len(hits()) >= n, timeout)
        return hits()[n - 1]


# --------------------------------------------------------------------- fake hub


class FakeHub:
    """Odysseus as the K230 sees it. Modes: `old_hub` (only /api/codex/sound/*), `revoked`
    (every token route answers 401), `module_down` (the Athena routes answer 502), `drop`
    (connections are closed without an answer: a hub that isn't reachable)."""

    def __init__(self) -> None:
        self.athena = FakeApiServer()
        self.athena.close()  # only its route() is used
        self.ody = FakeOdysseus()
        self.ody.close()
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.codes: dict[str, float] = {CODE: time.time() + 600}
        self.redeemed: set[str] = set()
        self.old_hub = False
        self.revoked = False
        self.module_down = False
        self.drop = False
        self.rate_limited = False
        self.lock = threading.Lock()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _handle(self, method: str) -> None:
                if hub.drop:
                    self.close_connection = True
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(2)
                    return
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                with hub.lock:
                    status, answer = hub.route(method, self.path, dict(self.headers.items()),
                                               body)
                out = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain" if isinstance(answer, str)
                                 else "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def route(self, method: str, raw: str, headers: dict[str, str],
              body: Any) -> tuple[int, Any]:
        self.requests.append((method, raw, headers, body))
        url = urllib.parse.urlsplit(raw)  # an absolute URL when it came through a proxy
        path = url.path
        query = f"?{url.query}" if url.query else ""
        if method == "POST" and path == "/api/pair/redeem":
            return self._redeem(body)
        if self.revoked or headers.get("Authorization") != f"Bearer {HUB_TOKEN}":
            return 401, {"detail": "Invalid or expired API token"}
        for new, old in (("/api/codex/athena/", "/api/codex/sound/"),):
            prefix = None
            if path.startswith(new) and not self.old_hub:
                prefix = new
            elif path.startswith(old):
                prefix = old
            elif path.startswith(new):
                return 404, {"detail": "Not Found"}
            if prefix is not None:
                if self.module_down:
                    return 502, {"detail": "Athena is not reachable"}
                rest = path[len(prefix):]
                mod = "/health" if rest == "health" else f"/v1/{rest}"
                return self.athena.route(method, mod + query, body, f"Bearer {TOKEN}")
        return self.ody.route(method, path + query, {"Authorization": f"Bearer {ODY_TOKEN}"},
                              body)

    def _redeem(self, body: Any) -> tuple[int, Any]:
        if self.rate_limited:
            return 429, {"detail": "Too many pairing attempts; wait a few minutes."}
        code = (body or {}).get("code", "")
        if code not in self.codes or code in self.redeemed or self.codes[code] <= time.time():
            return 400, {"detail": "This pairing code is not valid any more: make a new "
                                   "one in Odysseus (Settings → Integrations → Pair a phone)."}
        self.redeemed.add(code)
        return 200, {"url": self.url, "token": HUB_TOKEN, "name": "Phone relay 2026-11-13 09:00",
                     "scopes": ["athena:ingest", "athena:read", "athena:write"]}

    def paths(self, method: str | None = None) -> list[str]:
        return [urllib.parse.urlsplit(p).path for m, p, _, _ in self.requests
                if method is None or m == method]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeTailnet:
    """Stands in for tailnet.Tailnet: `poll()` returns `state`."""

    def __init__(self, state=None) -> None:
        from athena_pda_sync.tailnet import TailnetState

        self.state = state or TailnetState("running", None, "athena-pda.tail0000.ts.net")
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.state
