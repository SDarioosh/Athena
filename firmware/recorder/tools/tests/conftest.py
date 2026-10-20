"""Test helpers: a fake watch (the watch side of the BLE protocol, in memory) and a fake
server (http.server in a thread). Only fictional data: random bytes stand in for audio."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from barvis_relay import drain as d  # noqa: E402
from barvis_relay import protocol as p  # noqa: E402

TOKEN = "fictional-test-token-7f3a9c"
T0_MS = 1_792_411_200_000  # 2026-10-19T12:00:00Z


# --------------------------------------------------------------------- fake watch


@dataclass
class FakeChunk:
    chunk_id: int
    session_id: bytes
    sequence: int
    start_ms: int
    data: bytes
    flags: int = 0
    duration_ms: int = 30_000
    same_boot: bool = True  # a TIME_UNSET chunk from this boot gets fixed by TIME
    unset_session_start_ms: int = 0  # the epoch the watch would compute on TIME

    def entry(self) -> p.Entry:
        return p.Entry(self.chunk_id, self.session_id, self.sequence, self.start_ms,
                       self.duration_ms, len(self.data), zlib.crc32(self.data), self.flags)


@dataclass
class FakeWatch:
    """The watch side of the protocol, implementing drain.Transport."""

    chunks: dict[int, FakeChunk] = field(default_factory=dict)
    mtu: int = 247
    info: bytes = b"proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra"
    clock_ms: int | None = None
    # fault injection
    busy_next: int = 0  # answer ERROR busy to the next N commands
    corrupt_reads: dict[int, int] = field(default_factory=dict)  # chunk -> reads to corrupt
    drop_after_frames: dict[int, int] = field(default_factory=dict)  # chunk -> frames, once
    read_io_error: set[int] = field(default_factory=set)
    vanish: set[int] = field(default_factory=set)  # READ answers not_found
    drop_on_ack: set[int] = field(default_factory=set)  # link drops instead of ACKED, once
    # observations
    commands: list[bytes] = field(default_factory=list)
    acked: list[int] = field(default_factory=list)
    reads: list[tuple[int, int]] = field(default_factory=list)  # (chunk, offset)
    events: list[tuple] = field(default_factory=list)  # shared with FakeServer.events
    connected: bool = True

    def __post_init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def add(self, chunk: FakeChunk) -> None:
        self.chunks[chunk.chunk_id] = chunk

    def reconnect(self) -> None:
        self.queue = asyncio.Queue()
        self.connected = True

    # Transport
    async def read_info(self) -> bytes:
        self._check()
        return self.info

    async def read_status(self) -> bytes:
        self._check()
        queued = list(self.chunks.values())
        return p.Status(1, p.ST_SD_OK | p.ST_TIME_VALID, 80, 3900, len(queued),
                        sum(len(c.data) for c in queued), 30_000 * len(queued),
                        (self.clock_ms or 0) // 1000, 0x01020304).encode()

    async def write(self, command: bytes) -> None:
        self._check()
        self.commands.append(bytes(command))
        if self.busy_next:
            self.busy_next -= 1
            self._send(p.Error(command[0], p.ERR_BUSY))
            return
        if not self.queue.empty():  # previous command's answer not consumed: still "running"
            self._send(p.Error(command[0], p.ERR_BUSY))
            return
        if self.mtu < p.MIN_MTU:
            self._send(p.Error(command[0], p.ERR_MTU_TOO_SMALL, self.mtu))
            return
        handler = {p.OP_LIST: self._list, p.OP_READ: self._read, p.OP_ACK: self._ack,
                   p.OP_TIME: self._time, p.OP_BYE: self._bye}.get(command[0])
        if handler is None:
            self._send(p.Error(command[0], p.ERR_UNKNOWN_CMD))
        else:
            handler(bytes(command))

    async def receive(self, timeout: float) -> bytes:
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if item is None:
            self.connected = False
            raise d.LinkLost("fake disconnect")
        return item

    def discard_pending(self) -> int:
        n = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            n += 1
        return n

    # watch side
    def _check(self) -> None:
        if not self.connected:
            raise d.LinkLost("not connected")

    def _send(self, msg) -> None:
        self.queue.put_nowait(msg.encode())

    def _list(self, cmd: bytes) -> None:
        after = int.from_bytes(cmd[1:5], "little")
        mx = min(int.from_bytes(cmd[5:7], "little"), 32)
        ids = sorted(i for i in self.chunks if i > after)
        for i in ids[:mx]:
            self._send(self.chunks[i].entry())
        self._send(p.ListEnd(len(ids[:mx]), max(0, len(ids) - mx)))

    def _read(self, cmd: bytes) -> None:
        cid, offset, length = (int.from_bytes(cmd[i:i + 4], "little") for i in (1, 5, 9))
        self.reads.append((cid, offset))
        c = self.chunks.get(cid)
        if c is None or cid in self.vanish:
            self._send(p.Error(p.OP_READ, p.ERR_NOT_FOUND, cid))
            return
        data = bytearray(c.data)
        if self.corrupt_reads.get(cid):
            self.corrupt_reads[cid] -= 1
            data[len(data) // 2] ^= 0xFF
        end = len(data) if length == 0 else min(len(data), offset + length)
        step = p.data_payload_max(self.mtu)
        for frames, off in enumerate(range(offset, end, step)):
            if cid in self.drop_after_frames and frames == self.drop_after_frames[cid]:
                del self.drop_after_frames[cid]
                self.queue.put_nowait(None)
                return
            self._send(p.DataFrame(cid, off, bytes(data[off:min(end, off + step)])))
        status = 1 if cid in self.read_io_error else 0
        self._send(p.ReadEnd(status, cid, len(c.data), zlib.crc32(c.data)))

    def _ack(self, cmd: bytes) -> None:
        cid = int.from_bytes(cmd[1:5], "little")
        if cid in self.drop_on_ack:
            self.drop_on_ack.discard(cid)
            self.queue.put_nowait(None)
            return
        self.acked.append(cid)
        self.events.append(("ack", cid))
        self._send(p.Acked(0 if self.chunks.pop(cid, None) else 1, cid))

    def _time(self, cmd: bytes) -> None:
        epoch_ms = int.from_bytes(cmd[4:12], "little")
        if not 1_704_067_200_000 <= epoch_ms < 4_102_444_800_000:
            self._send(p.Error(p.OP_TIME, p.ERR_BAD_TIME))
            return
        self.clock_ms = epoch_ms
        fixed = 0
        for c in self.chunks.values():
            if c.flags & p.FLAG_TIME_UNSET and c.same_boot:
                c.start_ms += c.unset_session_start_ms
                c.flags &= ~p.FLAG_TIME_UNSET
                fixed += 1
        self._send(p.TimeOk(fixed))

    def _bye(self, cmd: bytes) -> None:
        self._send(p.ByeOk())

    def opcodes(self) -> list[int]:
        return [c[0] for c in self.commands]


def make_session(watch: FakeWatch, n: int, *, first_id: int = 1, session: bytes | None = None,
                 start_ms: int = T0_MS, size: int = 3000, last: bool = True,
                 flags: int = 0, rng: random.Random | None = None) -> list[FakeChunk]:
    rng = rng or random.Random(n * 7919 + first_id)
    session = session or bytes(rng.getrandbits(8) for _ in range(8))
    out = []
    for k in range(n):
        f = flags | (p.FLAG_LAST if last and k == n - 1 else 0)
        c = FakeChunk(first_id + k, session, k, start_ms + 30_000 * k,
                      b"OggS" + rng.randbytes(size - 4 + k), f)
        watch.add(c)
        out.append(c)
    return out


# --------------------------------------------------------------------- fake server


@dataclass
class Req:
    method: str
    path: str
    headers: dict[str, str]
    fields: dict[str, str]
    files: dict[str, tuple[str, str, bytes]]  # name -> (filename, content type, data)


class FakeServer:
    """Records requests; answers from `plan` (FIFO of actions) or ok.

    Actions: ("ok",), ("dup",), (status, detail), ("timeout", seconds)."""

    def __init__(self) -> None:
        self.requests: list[Req] = []
        self.plan: list[tuple] = []
        self.end_plan: list[tuple] = []
        self.health_status = 200
        self.events: list[tuple] = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _answer(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                server.record(self, b"")
                if self.path == "/health":
                    self._answer(server.health_status, {"ok": server.health_status == 200})
                else:
                    self._answer(404, {"detail": "not found"})

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req = server.record(self, body)
                if req.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._answer(401, {"detail": "invalid or missing bearer token"})
                    return
                plan = server.end_plan if self.path.endswith("/end") else server.plan
                with server.lock:
                    action = plan.pop(0) if plan else ("ok",)
                if action[0] == "timeout":
                    time.sleep(action[1])
                    action = ("ok",)
                if action[0] == "redirect":
                    self.send_response(307)
                    self.send_header("Location", action[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if action[0] in ("ok", "dup"):
                    self._answer(200, {"ok": True, "stop": False, "reason": None,
                                       "duplicate": action[0] == "dup"})
                else:
                    self._answer(action[0], {"detail": action[1]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,),
                                       daemon=True)
        self.thread.start()

    def record(self, h: BaseHTTPRequestHandler, body: bytes) -> Req:
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, str, bytes]] = {}
        ctype = h.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=HTTP).parsebytes(
                f"Content-Type: {ctype}\r\n\r\n".encode() + body)
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True)
                if part.get_filename():
                    files[name] = (part.get_filename(), part.get_content_type(), payload)
                else:
                    fields[name] = payload.decode()
        req = Req(h.command, h.path, dict(h.headers.items()), fields, files)
        with self.lock:
            self.requests.append(req)
            self.events.append(("http", h.path))
        return req

    def ingests(self) -> list[Req]:
        return [r for r in self.requests if r.path == "/v1/ingest/chunk"]

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_env_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    os.environ.pop("CONVO_INGEST_TOKEN", None)
