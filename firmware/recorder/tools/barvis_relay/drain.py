"""The relay algorithm of docs/twatch-ble-protocol.md §6, over an abstract transport.

`Relay.run(transport)` handles one connection: read `info` and `status`, send `TIME`, check
the server, then `LIST` / `READ` / upload / (`/end`) / `ACK` every chunk, and `BYE`. The
transport is the BLE link (bleak in ble_relay.py, a fake watch in the tests). A `Relay`
lives across connections so a read cut short by a disconnect resumes at its offset.

Delivery rule (§1): a chunk is acked only after the server answered `ok` for it and, for a
session's last chunk, `/end` answered `ok` too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import protocol as p
from .anchors import Anchors
from .uploader import AUTH, SKIP, Uploader, UploadError

log = logging.getLogger("barvis_relay")

# Modes
UPLOAD = "upload"
DRY_RUN = "dry_run"  # read and verify, optionally save; no upload, no ACK
LIST_ONLY = "list"  # info, status and LIST only; no TIME, nothing changes on the watch

# Outcomes of one connection
DONE = "done"  # drained (some chunks may have been skipped)
SERVER_RETRY = "server_retry"  # server unreachable / 5xx / timeout: retry with backoff
AUTH_FAILED = "auth_failed"  # 401/403: stop until the token changes
WATCH_RETRY = "watch_retry"  # watch SD error, MTU too small, protocol error: retry later
LINK_LOST = "link_lost"  # disconnected or the watch stopped answering
INCOMPATIBLE = "incompatible"  # info.proto != 1

MAX_REREADS = 3  # §5.2: re-read a chunk that fails verification at most 3 times
BUSY_RETRIES = 3
BUSY_DELAY = 0.5
BACKOFF_SECONDS = (60, 120, 300, 600, 900)  # §6 step 5: 1, 2, 5, 10, 15 min


class LinkLost(Exception):
    """The BLE link dropped (or a GATT operation failed)."""


class WatchError(Exception):
    """The watch answered ERROR, or a read stopped early."""

    def __init__(self, message: str, error: p.Error | None = None) -> None:
        super().__init__(message)
        self.error = error


class Transport(Protocol):
    async def read_info(self) -> bytes: ...

    async def read_status(self) -> bytes: ...

    async def write(self, command: bytes) -> None:
        """Write one command to `ctrl` (Write Request). Raises LinkLost."""

    async def receive(self, timeout: float) -> bytes:
        """Next `data` notification. Raises LinkLost, or TimeoutError after `timeout`."""

    def discard_pending(self) -> int:
        """Drop notifications received but not yet consumed; return how many."""


@dataclass
class Report:
    outcome: str = DONE
    message: str = ""
    info: p.Info | None = None
    status: p.Status | None = None
    time_fixed: int | None = None
    listed: list[p.Entry] = field(default_factory=list)
    uploaded: list[int] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    acked: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)
    saved: list[Path] = field(default_factory=list)
    bytes_read: int = 0


@dataclass
class _Partial:
    key: tuple[bytes, int, int, int]
    buf: bytearray


class Backoff:
    """Retry delays after server or watch errors: 1, 2, 5, 10, 15, 15, ... minutes."""

    def __init__(self, steps: tuple[int, ...] = BACKOFF_SECONDS) -> None:
        self.steps = steps
        self.failures = 0

    def next(self) -> int:
        delay = self.steps[min(self.failures, len(self.steps) - 1)]
        self.failures += 1
        return delay

    def reset(self) -> None:
        self.failures = 0


class Relay:
    def __init__(self, uploader: Uploader | None, anchors: Anchors, *, mode: str = UPLOAD,
                 save_dir: Path | None = None, clock: Callable[[], float] = time.time,
                 timeout: float = 10.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if mode == UPLOAD and uploader is None:
            raise ValueError("upload mode needs an uploader")
        self.uploader = uploader
        self.anchors = anchors
        self.mode = mode
        self.save_dir = save_dir
        self.clock = clock
        self.timeout = timeout
        self._sleep = sleep
        self.partial: dict[int, _Partial] = {}

    # ------------------------------------------------------------ connection

    async def run(self, t: Transport) -> Report:
        """Handle one connection. Never raises for link, watch or server trouble."""
        report = Report()
        try:
            await self._session(t, report)
        except LinkLost as e:
            report.outcome, report.message = LINK_LOST, f"link lost: {e}"
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 (distinct on Python 3.10)
            report.outcome = LINK_LOST
            report.message = f"the watch didn't answer within {self.timeout:g} s"
        except p.ProtocolError as e:
            report.outcome, report.message = WATCH_RETRY, f"protocol error: {e}"
        except WatchError as e:
            report.outcome, report.message = WATCH_RETRY, str(e)
            if e.error and e.error.code == p.ERR_MTU_TOO_SMALL:
                report.message = (f"ATT MTU is {e.error.arg}, the watch needs >= {p.MIN_MTU}; "
                                  "check the Bluetooth adapter/driver")
            await self._bye(t)  # the command finished, the link is still usable
        if report.outcome != DONE:
            log.log(logging.ERROR if report.outcome in (AUTH_FAILED, INCOMPATIBLE)
                    else logging.WARNING, "%s", report.message)
        return report

    async def _session(self, t: Transport, report: Report) -> None:
        info = p.Info.parse(await t.read_info())
        report.info = info
        log.info("watch: device=%s fw=%s board=%s proto=%d",
                 info.device, info.fw or "?", info.board or "?", info.proto)
        if info.proto != p.PROTO_VERSION:
            report.outcome = INCOMPATIBLE
            report.message = (f"watch speaks protocol {info.proto}, this relay only "
                              f"{p.PROTO_VERSION}; update the relay or the firmware")
            await self._bye(t)
            return
        try:
            report.status = p.Status.decode(await t.read_status())
            log.info("status: %s", report.status.describe())
        except p.ProtocolError as e:
            log.warning("can't parse status: %s", e)

        if self.mode != LIST_ONLY:
            now_ms = int(self.clock() * 1000)
            ok = await self._expect(t, p.encode_time(now_ms), p.TimeOk)
            report.time_fixed = ok.fixed
            log.info("TIME %s sent%s", p.iso_ms(now_ms),
                     f", {ok.fixed} TIME_UNSET chunk(s) fixed" if ok.fixed else "")

        if self.mode == UPLOAD:
            assert self.uploader is not None
            try:
                await asyncio.to_thread(self.uploader.health)
            except UploadError as e:
                report.outcome = SERVER_RETRY
                report.message = f"server not reachable, nothing pulled: {e}"
                await self._bye(t)
                return

        await self._drain(t, info, report)
        if report.outcome == DONE:
            log.info("done: %d listed, %d uploaded (%d duplicate), %d acked, %d skipped",
                     len(report.listed), len(report.uploaded), len(report.duplicates),
                     len(report.acked), len(report.skipped))
        await self._bye(t)

    async def _drain(self, t: Transport, info: p.Info, report: Report) -> None:
        after = 0
        while True:
            entries, end = await self._list(t, after)
            for e in entries:
                after = max(after, e.chunk_id)
                report.listed.append(e)
                if self.mode == LIST_ONLY:
                    continue
                if await self._chunk(t, info, e, report):
                    return  # stop draining (server trouble or token rejected)
            if not entries:
                if end.remaining:
                    log.warning("LIST returned nothing but says %d remaining", end.remaining)
                return

    # ------------------------------------------------------------ one chunk

    async def _chunk(self, t: Transport, info: p.Info, e: p.Entry, report: Report) -> bool:
        """Read, upload, end and ack one chunk. Returns True to stop draining."""
        name = (f"chunk {e.chunk_id} ({e.session_hex}#{e.sequence}"
                f"{' LAST' if e.last else ''}{' NOTE' if e.note else ''})")
        data = await self._read_verified(t, e, report, name)
        if data is None:
            return False
        timestamp = self._timestamp(e)

        if self.mode == DRY_RUN:
            if self.save_dir is not None:
                dest = self.save_dir / f"{e.session_hex}-{e.sequence}.opus"
                dest.write_bytes(data)
                report.saved.append(dest)
            log.info("%s: %d B, %s, verified (dry run, not uploaded or acked)",
                     name, len(data), timestamp)
            self.partial.pop(e.chunk_id, None)
            return False

        assert self.uploader is not None
        try:
            answer = await asyncio.to_thread(
                self.uploader.upload_chunk, device=info.device, session_id=e.session_hex,
                sequence=e.sequence, timestamp=timestamp, audio=data,
                kind="note" if e.note else None)
            report.uploaded.append(e.chunk_id)
            if answer.get("duplicate"):
                report.duplicates.append(e.chunk_id)
            log.info("%s: %d B, %s -> server ok%s", name, len(data), timestamp,
                     " (duplicate)" if answer.get("duplicate") else "")
            if e.last:
                await asyncio.to_thread(self.uploader.end_session, e.session_hex)
                log.info("session %s ended on the server", e.session_hex)
        except UploadError as err:
            if err.kind == SKIP:
                self.partial.pop(e.chunk_id, None)
                report.skipped[e.chunk_id] = str(err)
                log.error("%s: server refused it, left on the watch and skipped: %s "
                          "(drop it on the watch with `queue drop %d` if it's bad)",
                          name, err, e.chunk_id)
                return False
            if err.kind == AUTH:
                report.outcome = AUTH_FAILED
                report.message = f"token rejected by the server: {err}"
            else:
                report.outcome = SERVER_RETRY
                report.message = f"server error, stopping, nothing more acked: {err}"
            return True

        await self._expect(t, p.encode_ack(e.chunk_id), p.Acked, chunk_id=e.chunk_id)
        report.acked.append(e.chunk_id)
        self.partial.pop(e.chunk_id, None)
        log.debug("%s acked", name)
        return False

    def _timestamp(self, e: p.Entry) -> str:
        if not e.time_unset:
            return p.iso_ms(e.start_ms)
        anchor = self.anchors.anchor_ms(e.session_hex)
        log.warning("chunk %d is still TIME_UNSET; timestamp = relay anchor + %d ms",
                    e.chunk_id, e.start_ms)
        return p.iso_ms(anchor + e.start_ms)

    async def _read_verified(self, t: Transport, e: p.Entry, report: Report,
                             name: str) -> bytes | None:
        key = (e.session_id, e.sequence, e.size, e.crc32)
        part = self.partial.get(e.chunk_id)
        if part is None or part.key != key or len(part.buf) > e.size:
            part = _Partial(key, bytearray())
            self.partial[e.chunk_id] = part
        elif part.buf:
            log.info("%s: resuming read at byte %d of %d", name, len(part.buf), e.size)
        rereads = 0
        while True:
            try:
                problem = await self._read(t, e, part.buf, report)
            except WatchError as err:
                if err.error and err.error.code == p.ERR_NOT_FOUND:
                    report.skipped[e.chunk_id] = "not on the watch any more"
                    log.warning("%s: watch says not_found, skipping", name)
                    self.partial.pop(e.chunk_id, None)
                    return None
                raise
            if problem is None:
                return bytes(part.buf)
            part.buf.clear()
            if rereads >= MAX_REREADS:
                report.skipped[e.chunk_id] = f"failed verification: {problem}"
                log.error("%s: failed verification %d times (%s); skipped for this "
                          "connection", name, rereads + 1, problem)
                self.partial.pop(e.chunk_id, None)
                return None
            rereads += 1
            log.warning("%s: %s; reading it again (%d/%d)", name, problem, rereads, MAX_REREADS)

    async def _read(self, t: Transport, e: p.Entry, buf: bytearray,
                    report: Report) -> str | None:
        """READ from len(buf) to the end, appending to buf. Returns a problem or None."""
        bad: list[str] = []
        end: list[p.ReadEnd] = []

        def on(msg: p.Response) -> bool:
            if isinstance(msg, p.DataFrame):
                if msg.chunk_id != e.chunk_id:
                    bad.append(f"DATA for chunk {msg.chunk_id}")
                elif msg.offset != len(buf):
                    bad.append(f"DATA at offset {msg.offset}, expected {len(buf)}")
                if not bad:
                    buf.extend(msg.payload)
                    report.bytes_read += len(msg.payload)
                    if len(buf) > e.size:
                        bad.append(f"more than ENTRY.bytes ({e.size})")
                return False
            if isinstance(msg, p.ReadEnd):
                end.append(msg)
                return True
            raise p.ProtocolError(f"unexpected {type(msg).__name__} during READ")

        await self._exchange(t, p.encode_read(e.chunk_id, len(buf), 0), on)
        r = end[0]
        if r.chunk_id != e.chunk_id:
            raise p.ProtocolError(f"READ_END for chunk {r.chunk_id}, expected {e.chunk_id}")
        if r.status != 0:
            raise WatchError(f"READ of chunk {e.chunk_id} stopped early (status {r.status}, "
                             "SD error on the watch); retrying later")
        if bad:
            return bad[0]
        if r.total_bytes != e.size or len(buf) != e.size:
            return f"size {len(buf)} (READ_END {r.total_bytes}), ENTRY says {e.size}"
        crc = zlib.crc32(buf) & 0xFFFFFFFF
        if crc != e.crc32 or r.crc32 != e.crc32:
            return f"CRC-32 {crc:08x} (READ_END {r.crc32:08x}), ENTRY says {e.crc32:08x}"
        return None

    # ------------------------------------------------------------ commands

    async def _list(self, t: Transport, after: int) -> tuple[list[p.Entry], p.ListEnd]:
        entries: list[p.Entry] = []
        end: list[p.ListEnd] = []

        def on(msg: p.Response) -> bool:
            if isinstance(msg, p.Entry):
                if msg.chunk_id <= max([after] + [x.chunk_id for x in entries[-1:]]):
                    raise p.ProtocolError(f"LIST entry {msg.chunk_id} out of order")
                entries.append(msg)
                return False
            if isinstance(msg, p.ListEnd):
                end.append(msg)
                return True
            raise p.ProtocolError(f"unexpected {type(msg).__name__} during LIST")

        await self._exchange(t, p.encode_list(after, p.LIST_MAX), on)
        if end[0].count != len(entries):
            log.warning("LIST_END says %d entries, got %d", end[0].count, len(entries))
        return entries, end[0]

    async def _expect(self, t: Transport, command: bytes, kind: type, *,
                      chunk_id: int | None = None) -> Any:
        got: list[Any] = []

        def on(msg: p.Response) -> bool:
            if not isinstance(msg, kind):
                raise p.ProtocolError(f"expected {kind.__name__}, got {type(msg).__name__}")
            if chunk_id is not None and msg.chunk_id != chunk_id:
                raise p.ProtocolError(f"{kind.__name__} for chunk {msg.chunk_id}, "
                                      f"expected {chunk_id}")
            got.append(msg)
            return True

        await self._exchange(t, command, on)
        return got[0]

    async def _exchange(self, t: Transport, command: bytes,
                        on: Callable[[p.Response], bool]) -> None:
        """Write one command and feed its responses to `on` until it returns True.

        ERROR ends the command (WatchError). `busy` as the first answer means an earlier
        command is still running: wait, drop its leftovers and try again a few times."""
        for attempt in range(BUSY_RETRIES + 1):
            await t.write(command)
            first = True
            while True:
                msg = p.decode_response(await t.receive(self.timeout))
                if isinstance(msg, p.Error):
                    if msg.code == p.ERR_BUSY and first and attempt < BUSY_RETRIES:
                        break
                    raise WatchError(str(msg), msg)
                first = False
                if on(msg):
                    return
            log.debug("watch busy, retrying %s", p.COMMAND_NAMES.get(command[0], "?"))
            await self._sleep(BUSY_DELAY)
            t.discard_pending()
        raise AssertionError("unreachable")

    async def _bye(self, t: Transport) -> None:
        with contextlib.suppress(LinkLost, WatchError, p.ProtocolError,
                                 asyncio.TimeoutError, TimeoutError):  # noqa: UP041
            await self._expect(t, p.encode_bye(), p.ByeOk)
