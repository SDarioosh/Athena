"""Test helpers for the T5 PDA: a fake T5 (the T5 side of the glance protocol, in memory)
and a fake odysseus-sound API (http.server in a thread). Fictional data only."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from barvis_relay import pda_protocol as q
from barvis_relay.drain import LinkLost
from barvis_relay.pda_actions import CalAdd
from barvis_relay.uploader import UploadError, classify

TOKEN = "fictional-pda-token-51f4"
NOW = 1_792_486_800.0  # 2026-10-20T09:00:00Z


# --------------------------------------------------------------------- fake T5


@dataclass
class FakeT5:
    """The T5 side of the protocol, implementing pda_sync.Transport."""

    mtu: int = 517
    info: bytes = b"proto=1;device=t5-pda;fw=0.1.0;board=t5_epaper_pro"
    flags: int = q.ST_TIME_VALID
    outbox: dict[int, tuple[int, bytes]] = field(default_factory=dict)  # id -> (ms, json)
    feed: bytes | None = None
    feed_version: int = 0
    details: dict[int, bytes] = field(default_factory=dict)  # blob id -> bytes
    clock_ms: int | None = None
    tz_offset_min: int | None = None
    # fault injection
    bad_crc_next: int = 0  # corrupt the next N PUTs (PUT_END answers bad_crc)
    drop_on_ack: set[int] = field(default_factory=set)  # link drops instead of OB_ACKED, once
    busy_next: int = 0
    # observations
    commands: list[bytes] = field(default_factory=list)
    acks: list[tuple[int, int]] = field(default_factory=list)  # (action id, result)
    puts: list[tuple[int, int]] = field(default_factory=list)  # (kind, blob id) completed
    connected: bool = True
    _next_id: int = 1

    def __post_init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.status_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._put: dict[str, Any] | None = None

    def queue_action(self, action: dict[str, Any] | bytes, created_ms: int = 0) -> int:
        aid = self._next_id
        self._next_id += 1
        raw = action if isinstance(action, bytes) else json.dumps(action).encode()
        self.outbox[aid] = (created_ms or int(NOW * 1000), raw)
        return aid

    def reconnect(self) -> None:
        self.queue = asyncio.Queue()
        self.status_queue = asyncio.Queue()
        self._put = None  # a disconnect mid-PUT discards it
        self.connected = True

    def status(self) -> q.Status:
        return q.Status(1, self.flags, 77, len(self.outbox), self.feed_version, 0x0A0B0C0D)

    def notify_status(self) -> None:
        self.status_queue.put_nowait(self.status().encode())

    def feed_json(self) -> dict[str, Any]:
        assert self.feed is not None
        return json.loads(self.feed)

    # Transport
    async def read_info(self) -> bytes:
        self._check()
        return self.info

    async def read_status(self) -> bytes:
        self._check()
        return self.status().encode()

    async def wait_status(self, timeout: float) -> bytes:
        self._check()
        item = await asyncio.wait_for(self.status_queue.get(), timeout)
        if item is None:
            raise LinkLost("fake disconnect")
        return item

    async def write(self, command: bytes) -> None:
        self._check()
        if len(command) > self.mtu - 3:
            raise LinkLost(f"write of {len(command)} B with MTU {self.mtu}")
        self.commands.append(bytes(command))
        op = command[0] if command else 0
        if self.busy_next and op != q.OP_PUT_DATA:
            self.busy_next -= 1
            self._send(q.Error(op, q.ERR_BUSY))
            return
        if self.mtu < q.MIN_MTU:
            self._send(q.Error(op, q.ERR_MTU_TOO_SMALL, self.mtu))
            return
        handler = {q.OP_TIME: self._time, q.OP_PUT_BEGIN: self._put_begin,
                   q.OP_PUT_DATA: self._put_data, q.OP_PUT_END: self._put_end,
                   q.OP_OB_LIST: self._ob_list, q.OP_OB_ACK: self._ob_ack,
                   q.OP_BYE: self._bye}.get(op)
        if handler is None:
            self._send(q.Error(op, q.ERR_UNKNOWN_CMD))
        else:
            handler(bytes(command))

    async def receive(self, timeout: float) -> bytes:
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if item is None:
            self.connected = False
            raise LinkLost("fake disconnect")
        return item

    def discard_pending(self) -> int:
        n = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            n += 1
        return n

    # T5 side
    def _check(self) -> None:
        if not self.connected:
            raise LinkLost("not connected")

    def _send(self, msg) -> None:
        self.queue.put_nowait(msg.encode())

    def _time(self, cmd: bytes) -> None:
        if len(cmd) != 14:
            self._send(q.Error(q.OP_TIME, q.ERR_BAD_LENGTH))
            return
        epoch_ms = int.from_bytes(cmd[4:12], "little")
        if not 1_704_067_200_000 <= epoch_ms < 4_102_444_800_000:
            self._send(q.Error(q.OP_TIME, q.ERR_BAD_TIME))
            return
        self.clock_ms = epoch_ms
        self.tz_offset_min = int.from_bytes(cmd[12:14], "little", signed=True)
        self._send(q.TimeOk())

    def _put_begin(self, cmd: bytes) -> None:
        if len(cmd) != 16:
            self._send(q.Error(q.OP_PUT_BEGIN, q.ERR_BAD_LENGTH))
            return
        kind, blob_id = cmd[1], int.from_bytes(cmd[4:8], "little")
        total, crc = int.from_bytes(cmd[8:12], "little"), int.from_bytes(cmd[12:16], "little")
        if total > q.MAX_BLOB:
            self._send(q.Error(q.OP_PUT_BEGIN, q.ERR_TOO_LARGE, total))
            return
        self._put = {"kind": kind, "id": blob_id, "total": total, "crc": crc,
                     "buf": bytearray()}
        self._send(q.PutReady(kind, blob_id))

    def _put_data(self, cmd: bytes) -> None:
        if self._put is None:
            self._send(q.Error(q.OP_PUT_DATA, q.ERR_BAD_STATE))
            return
        off = int.from_bytes(cmd[1:5], "little")
        buf = self._put["buf"]
        if off != len(buf):
            self._send(q.Error(q.OP_PUT_DATA, q.ERR_BAD_STATE, off))
            return
        buf.extend(cmd[5:])

    def _put_end(self, cmd: bytes) -> None:
        put, self._put = self._put, None
        if put is None:
            self._send(q.Error(q.OP_PUT_END, q.ERR_BAD_STATE))
            return
        data = bytes(put["buf"])
        if self.bad_crc_next:
            self.bad_crc_next -= 1
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        if len(data) != put["total"] or zlib.crc32(data) != put["crc"]:
            self._send(q.Error(q.OP_PUT_END, q.ERR_BAD_CRC))
            return
        if put["kind"] == q.KIND_FEED:
            self.feed, self.feed_version = data, put["id"]
        else:
            self.details[put["id"]] = data
        self.puts.append((put["kind"], put["id"]))
        self._send(q.PutOk(put["kind"], put["id"]))

    def _ob_list(self, cmd: bytes) -> None:
        after = int.from_bytes(cmd[1:5], "little")
        mx = int.from_bytes(cmd[5:7], "little")
        ids = sorted(i for i in self.outbox if i > after)
        for i in ids[:mx]:
            created, raw = self.outbox[i]
            self._send(q.ObEntry(i, created, raw))
        self._send(q.ObEnd(len(ids[:mx]), max(0, len(ids) - mx)))

    def _ob_ack(self, cmd: bytes) -> None:
        aid, result = int.from_bytes(cmd[1:5], "little"), cmd[5]
        if aid in self.drop_on_ack:
            self.drop_on_ack.discard(aid)
            self.queue.put_nowait(None)
            return
        self.acks.append((aid, result))
        self.outbox.pop(aid, None)
        self._send(q.ObAcked(result, aid))
        self.notify_status()  # §3.2: the T5 notifies status when the outbox changes

    def _bye(self, cmd: bytes) -> None:
        self._send(q.ByeOk())

    def opcodes(self) -> list[int]:
        return [c[0] for c in self.commands]


# --------------------------------------------------------------------- fake server


def demo_data() -> dict[str, Any]:
    """Fictional server state, in the shapes the odysseus-sound API returns."""
    return {
        "digests": {"2026-10-19": {
            "date": "2026-10-19", "headline": "Lease and family dinner",
            "summary": "Tom confirmed the new rent; Maria booked Casa Lupo.",
            "highlights": ["Rent goes up in January", "Dinner on November 8"],
            "open_action_items": [], "people": ["Tom", "Maria"]}},
        "conversations": [
            {"id": "c_new", "status": "summarized", "title": "Standup",
             "started_at": "2026-10-20T07:30:00+00:00", "people": ["Ben", "Alice"],
             "summary": "Ben finished the API migration.", "key_points": ["Demo moved"],
             "action_items": [{"id": 2, "text": "Review Ben's PR", "status": "open",
                               "due_date": None}]},
            {"id": "c_old", "status": "summarized", "title": "Lease renewal",
             "started_at": "2026-10-19T11:00:00+00:00", "people": ["Tom"],
             "summary": "Rent rises to 1520 euros.", "key_points": ["Sign by Oct 31"],
             "action_items": [{"id": 1, "text": "Sign the renewal", "status": "open",
                               "due_date": "2026-10-31"}]},
            {"id": "c_tv", "status": "discarded", "title": None, "started_at":
             "2026-10-19T21:00:00+00:00", "people": [], "summary": None, "key_points": []},
        ],
        "action_items": {
            1: {"id": 1, "conversation_id": "c_old", "text": "Sign the renewal",
                "owner": "Me", "due_date": "2026-10-31", "status": "open"},
            2: {"id": 2, "conversation_id": "c_new", "text": "Review Ben's PR",
                "owner": "Me", "due_date": None, "status": "open"},
            3: {"id": 3, "conversation_id": "c_new", "text": "Pick up Aunt Rosa",
                "owner": "Me", "due_date": "2026-11-08", "status": "open"},
        },
        "transcripts": {"c_new": [
            {"offset": float(o), "speaker": "Ben" if o % 16 else "Me",
             "is_owner": not o % 16, "text": f"line at {o} s"} for o in range(0, 1300, 8)]},
    }


class FakeApiServer:
    """The odysseus-sound routes the PDA uses, over HTTP. `fail` maps a path prefix to a
    list of statuses to answer (FIFO) before behaving normally."""

    def __init__(self) -> None:
        self.data = demo_data()
        self.requests: list[tuple[str, str, Any]] = []
        self.fail: dict[str, list[int]] = {}
        self.health_status = 200
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

            def _handle(self, method: str) -> None:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                status, answer = server.route(method, self.path, body,
                                              self.headers.get("Authorization"))
                self._answer(status, answer)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_POST(self) -> None:
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,),
                                       daemon=True)
        self.thread.start()

    def route(self, method: str, raw_path: str, body: Any, auth: str | None) -> tuple[int, dict]:
        url = urllib.parse.urlsplit(raw_path)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        with self.lock:
            self.requests.append((method, raw_path, body))
            if path == "/health":
                return self.health_status, {"ok": self.health_status == 200}
            if auth != f"Bearer {TOKEN}":
                return 401, {"detail": "invalid or missing bearer token"}
            for prefix, statuses in self.fail.items():
                if path.startswith(prefix) and statuses:
                    return statuses.pop(0), {"detail": "injected"}
            d = self.data
            parts = path.strip("/").split("/")[1:]  # drop "v1"
            if method == "GET" and parts[:1] == ["digests"]:
                dig = d["digests"].get(parts[1])
                return (200, dig) if dig else (404, {"detail": "no digest for that date"})
            if method == "POST" and parts[:1] == ["digests"]:
                if parts[1] not in ("2026-10-19", "2026-10-20"):
                    return 404, {"detail": "no summarized conversations that day"}
                d["digests"][parts[1]] = {"date": parts[1], "headline": "Rebuilt",
                                          "summary": "Fresh digest.", "highlights": []}
                return 200, d["digests"][parts[1]]
            if method == "GET" and parts == ["conversations"]:
                rows = d["conversations"][:int(query.get("limit", 50))]
                return 200, {"conversations": rows}
            if method == "GET" and parts[:1] == ["conversations"]:
                conv = next((c for c in d["conversations"] if c["id"] == parts[1]), None)
                if conv is None:
                    return 404, {"detail": "conversation not found"}
                if parts[2:] == ["transcript"]:
                    start = float(query.get("start", 0))
                    segs = [s for s in d["transcripts"].get(parts[1], [])
                            if s["offset"] >= start]
                    return 200, {"conversation_id": parts[1], "title": conv["title"],
                                 "segments": segs}
                return 200, conv
            if method == "GET" and parts == ["action-items"]:
                rows = [a for a in d["action_items"].values()
                        if a["status"] == query.get("status", "open")]
                return 200, {"action_items": rows}
            if method == "PATCH" and parts[:1] == ["action-items"]:
                item = d["action_items"].get(int(parts[1]))
                if item is None:
                    return 404, {"detail": "action item not found"}
                item["status"] = body["status"]
                return 200, {"ok": True}
            return 404, {"detail": "not found"}

    def calls(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.requests if method is None or m == method]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------- fake calendar


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {
            "ev1": {"id": "ev1", "title": "Dentist follow-up", "start": "2026-10-21T09:00:00Z",
                    "end": "2026-10-21T09:30:00Z", "where": "Dr. Patel", "all_day": False}}
        self.created: list[CalAdd] = []
        self.fail: list[UploadError] = []
        self._n = 1

    def _maybe_fail(self) -> None:
        if self.fail:
            raise self.fail.pop(0)

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        self._maybe_fail()
        return list(self.events.values())

    def create(self, event: CalAdd) -> str:
        self._maybe_fail()
        self._n += 1
        eid = f"ev{self._n}"
        self.created.append(event)
        self.events[eid] = {"id": eid, "title": event.title, "start": event.start,
                            "end": event.end, "where": None, "all_day": event.all_day}
        return eid

    def delete(self, event_id: str) -> None:
        self._maybe_fail()
        if self.events.pop(event_id, None) is None:
            raise UploadError(classify(404), "no such event", 404)
