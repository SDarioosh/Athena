"""The PDA relay algorithm of docs/t5-pda-plan.md §3.4, over an abstract transport.

`PdaSync.run(transport)` handles one connection to the T5: read `info` and `status`, send
`TIME`, check the server, then the outbox first (`OB_LIST`, server call, `OB_ACK`), then the
feed (`PUT` if its content changed or the T5 holds another version), then the detail blobs
of `fetch` actions, and `BYE`. With `stay=True` it keeps the link while `status` says the
user is active: outbox entries are handled within seconds and the feed is refreshed every
5 minutes.

Delivery rule (§3.3): an action is acked only after the server answered; a temporary
failure is never acked. A `fetch` is acked only after its detail blob got `PUT_OK`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import pda_protocol as q
from .drain import Backoff, LinkLost
from .pda_actions import STOP_AUTH, ActionRunner, Calendar, CalendarMemory, encode_detail
from .pda_feed import (
    FeedTooLarge,
    ServerApi,
    build_feed,
    content_hash,
    encode_feed,
    resolve_tz,
    tz_offset_minutes,
    weights_section,
)
from .uploader import AUTH, UploadError

log = logging.getLogger("barvis_relay.pda")

# Outcomes of one connection (the same words as drain.py)
DONE = "done"
SERVER_RETRY = "server_retry"
AUTH_FAILED = "auth_failed"
DEVICE_RETRY = "device_retry"  # T5 error, MTU too small, protocol error
LINK_LOST = "link_lost"
INCOMPATIBLE = "incompatible"

BUSY_RETRIES = 3
BUSY_DELAY = 0.5
PUT_ATTEMPTS = 3  # a blob whose PUT_END says bad_crc is sent again, 3 times in all
FEED_REFRESH_SECONDS = 300  # §3.4 step 6
STATUS_POLL_SECONDS = 30  # stay mode: re-read status this often without a notification


class PdaError(Exception):
    """The T5 answered ERROR."""

    def __init__(self, message: str, error: q.Error | None = None) -> None:
        super().__init__(message)
        self.error = error


class Transport(Protocol):
    mtu: int  # ATT MTU, 0 if the OS doesn't say

    async def read_info(self) -> bytes: ...

    async def read_status(self) -> bytes: ...

    async def write(self, command: bytes) -> None:
        """Write one command to `ctrl` (Write Request). Raises LinkLost."""

    async def receive(self, timeout: float) -> bytes:
        """Next `data` notification. Raises LinkLost, or TimeoutError after `timeout`."""

    def discard_pending(self) -> int:
        """Drop `data` notifications received but not yet consumed; return how many."""

    async def wait_status(self, timeout: float) -> bytes:
        """Next `status` notification. Raises LinkLost, or TimeoutError after `timeout`."""


# ------------------------------------------------------------------ state


class PdaState:
    """What the relay remembers per T5 across connections and runs (best effort, a small
    JSON file like anchors.json): the last feed pushed (hash, version), the calendar
    events created for cal_add actions and the hub's last good rule-weights table (the
    feed's `weights`, kept when a later fetch fails)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.feeds: dict[str, dict[str, Any]] = {}
        self.calendar = CalendarMemory(on_change=self.save)
        self.weights: dict[str, Any] | None = None
        if path is not None:
            try:
                raw = json.loads(path.read_text())
                self.feeds = dict(raw.get("feeds") or {})
                self.calendar.created = dict(raw.get("cal_created") or {})
                self.weights = weights_section(raw.get("weights"))  # checked again
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError) as e:
                log.warning("can't read %s (%s); starting empty", path, e)

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"feeds": self.feeds,
                                       "cal_created": self.calendar.created,
                                       "weights": self.weights}, indent=1))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("can't save %s (%s); keeping the state in memory", self.path, e)


# ------------------------------------------------------------------ report


@dataclass
class Report:
    outcome: str = DONE
    message: str = ""
    info: q.Info | None = None
    status: q.Status | None = None
    listed: list[q.ObEntry] = field(default_factory=list)
    acked: dict[int, int] = field(default_factory=dict)  # action id -> result
    waiting: dict[int, str] = field(default_factory=dict)  # left queued, why
    feed_pushed: list[int] = field(default_factory=list)  # versions
    feed_unchanged: int = 0
    details: list[str] = field(default_factory=list)  # refs PUT


@dataclass
class _Fetched:
    entry: q.ObEntry
    blob: dict[str, Any]
    result: int


class PdaSync:
    def __init__(self, api: ServerApi, *, tz_name: str = "UTC",
                 calendar: Calendar | None = None, templates: list[str] | None = None,
                 todos: Any = None, insight: Any = None,
                 state: PdaState | None = None,
                 device_key: str = "t5", stay: bool = False, list_only: bool = False,
                 clock: Callable[[], float] = time.time, timeout: float = 10.0,
                 feed_refresh: float = FEED_REFRESH_SECONDS,
                 status_poll: float = STATUS_POLL_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.api = api
        self.tz_name = tz_name
        self.tz = resolve_tz(tz_name)
        self.calendar = calendar
        self.templates = templates
        self.todos = todos
        self.insight = insight
        self.state = state or PdaState(None)
        self.runner = ActionRunner(api, calendar, self.state.calendar, todos)
        self.device_key = device_key
        self.stay = stay
        self.list_only = list_only
        self.clock = clock
        self.timeout = timeout
        self.feed_refresh = feed_refresh
        self.status_poll = status_poll
        self._sleep = sleep

    # ------------------------------------------------------------ connection

    async def run(self, t: Transport) -> Report:
        """Handle one connection. Never raises for link, T5 or server trouble."""
        report = Report()
        try:
            await self._session(t, report)
        except LinkLost as e:
            report.outcome, report.message = LINK_LOST, f"link lost: {e}"
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 (distinct on Python 3.10)
            report.outcome = LINK_LOST
            report.message = f"the T5 didn't answer within {self.timeout:g} s"
        except q.ProtocolError as e:
            report.outcome, report.message = DEVICE_RETRY, f"protocol error: {e}"
        except PdaError as e:
            report.outcome, report.message = DEVICE_RETRY, str(e)
            if e.error and e.error.code == q.ERR_MTU_TOO_SMALL:
                report.message = (f"ATT MTU is {e.error.arg}, the T5 needs >= {q.MIN_MTU}; "
                                  "check the Bluetooth adapter/driver")
            await self._bye(t)
        if report.outcome != DONE:
            log.log(logging.ERROR if report.outcome in (AUTH_FAILED, INCOMPATIBLE)
                    else logging.WARNING, "%s", report.message)
        return report

    async def _session(self, t: Transport, report: Report) -> None:
        info = q.Info.parse(await t.read_info())
        report.info = info
        log.info("T5: device=%s fw=%s board=%s proto=%d",
                 info.device, info.fw or "?", info.board or "?", info.proto)
        if info.proto != q.PROTO_VERSION:
            report.outcome = INCOMPATIBLE
            report.message = (f"the T5 speaks glance protocol {info.proto}, this relay only "
                              f"{q.PROTO_VERSION}; update the relay or the firmware")
            await self._bye(t)
            return
        if 0 < t.mtu < q.MIN_MTU:
            log.warning("ATT MTU is %d, the T5 needs >= %d; it will answer mtu_too_small",
                        t.mtu, q.MIN_MTU)
        report.status = await self._read_status(t)

        if self.list_only:  # look, change nothing: no TIME, no acks, no PUT
            await self._list_all(t, report)
            await self._bye(t)
            return

        now = self.clock()
        offset = tz_offset_minutes(self.tz, now)
        await self._expect(t, q.encode_time(int(now * 1000), offset), q.TimeOk)
        log.info("TIME sent (%s, UTC%+d min)", self.tz_name, offset)
        try:
            await asyncio.to_thread(self.api.health)
        except UploadError as e:  # §3.4 step 3: leave the feed as it is ("offline")
            report.outcome, report.message = SERVER_RETRY, f"server not reachable: {e}"
            await self._bye(t)
            return

        if await self._sync(t, report) and self.stay:
            await self._stay(t, report)
        if report.outcome == DONE:
            log.info("done: %d action(s) acked, %d waiting, feed %s, %d detail(s)",
                     len(report.acked), len(report.waiting),
                     f"v{report.feed_pushed[-1]} pushed" if report.feed_pushed
                     else "unchanged", len(report.details))
        await self._bye(t)

    async def _sync(self, t: Transport, report: Report) -> bool:
        """Outbox, feed, details. Returns False if it stopped on server trouble."""
        fetched = await self._outbox(t, report)
        if fetched is None:
            return False
        if not await self._feed(t, report):
            return False
        for f in fetched:
            data = encode_detail(f.blob)
            await self._put(t, q.KIND_DETAIL, f.entry.action_id, data)
            report.details.append(f.blob["ref"])
            log.info("detail %s: %d B sent", f.blob["ref"], len(data))
            await self._ack(t, f.entry.action_id, f.result, report)
        return True

    async def _stay(self, t: Transport, report: Report) -> None:
        """§3.4 step 6: while the user is active, react to the outbox, refresh the feed."""
        next_feed = self.clock() + self.feed_refresh
        status = report.status
        while status is not None and status.user_active:
            wait = max(0.0, min(self.status_poll, next_feed - self.clock()))
            try:
                status = q.Status.decode(await t.wait_status(wait))
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                status = await self._read_status(t)
            report.status = status
            if status is None:
                return
            refresh = self.clock() >= next_feed
            if status.outbox or refresh:
                if not await self._sync(t, report):
                    return
                next_feed = self.clock() + self.feed_refresh
                status = report.status = await self._read_status(t)
        log.info("the T5 is idle, disconnecting")

    async def _read_status(self, t: Transport) -> q.Status | None:
        try:
            status = q.Status.decode(await t.read_status())
        except q.ProtocolError as e:
            log.warning("can't parse status: %s", e)
            return None
        log.info("status: %s", status.describe())
        return status

    # ------------------------------------------------------------ outbox

    async def _list_all(self, t: Transport, report: Report) -> list[q.ObEntry]:
        after, out = 0, []
        while True:
            entries, end = await self._ob_list(t, after)
            out += entries
            report.listed += entries
            if not entries or not end.remaining:
                return out
            after = entries[-1].action_id

    async def _outbox(self, t: Transport, report: Report) -> list[_Fetched] | None:
        """Handle every queued action. Returns the fetched details still to send, or None
        if it stopped on server trouble (nothing after that point is acked)."""
        self.runner.begin_sync()
        fetched: list[_Fetched] = []
        after = 0
        while True:
            entries, end = await self._ob_list(t, after)
            for e in entries:
                after = e.action_id
                report.listed.append(e)
                outcome = await asyncio.to_thread(self.runner.run, e.action_id, e.json)
                log.info("action %d: %s", e.action_id, outcome.message)
                if outcome.stop:
                    report.outcome = AUTH_FAILED if outcome.stop == STOP_AUTH else SERVER_RETRY
                    report.message = outcome.message
                    return None
                if outcome.result is None:
                    report.waiting[e.action_id] = outcome.message
                elif outcome.detail is not None:
                    fetched.append(_Fetched(e, outcome.detail, outcome.result))
                else:
                    await self._ack(t, e.action_id, outcome.result, report)
            if not entries or not end.remaining:
                return fetched

    async def _ob_list(self, t: Transport, after: int) -> tuple[list[q.ObEntry], q.ObEnd]:
        entries: list[q.ObEntry] = []
        end: list[q.ObEnd] = []

        def on(msg: q.Response) -> bool:
            if isinstance(msg, q.ObEntry):
                if msg.action_id <= (entries[-1].action_id if entries else after):
                    raise q.ProtocolError(f"OB_ENTRY {msg.action_id} out of order")
                entries.append(msg)
                return False
            if isinstance(msg, q.ObEnd):
                end.append(msg)
                return True
            raise q.ProtocolError(f"unexpected {type(msg).__name__} during OB_LIST")

        await self._exchange(t, q.encode_ob_list(after, q.OB_LIST_MAX), on)
        if end[0].count != len(entries):
            log.warning("OB_END says %d entries, got %d", end[0].count, len(entries))
        return entries, end[0]

    async def _ack(self, t: Transport, action_id: int, result: int, report: Report) -> None:
        got = await self._expect(t, q.encode_ob_ack(action_id, result), q.ObAcked)
        if got.action_id != action_id:
            raise q.ProtocolError(f"OB_ACKED for {got.action_id}, expected {action_id}")
        report.acked[action_id] = result

    # ------------------------------------------------------------ feed

    async def _feed(self, t: Transport, report: Report) -> bool:
        now = self.clock()
        try:
            feed = await asyncio.to_thread(build_feed, self.api, now=now,
                                           tz_name=self.tz_name, calendar=self.calendar,
                                           templates=self.templates, todos=self.todos,
                                           insight=self.insight,
                                           last_weights=self.state.weights)
            if feed.get("weights") != self.state.weights:
                self.state.weights = feed.get("weights")
                self.state.save()
            data = encode_feed(feed)
        except UploadError as e:
            report.outcome = AUTH_FAILED if e.kind == AUTH else SERVER_RETRY
            report.message = f"can't build the feed, the T5 keeps its last one: {e}"
            return False
        except FeedTooLarge as e:
            report.outcome, report.message = SERVER_RETRY, str(e)
            return False
        digest = content_hash(feed)
        last = self.state.feeds.get(self.device_key, {})
        held = report.status.feed_version if report.status else None
        if last.get("hash") == digest and held == last.get("version"):
            report.feed_unchanged += 1
            log.info("feed unchanged (v%d on the T5), not sent", held)
            return True
        version = max(feed["version"], int(last.get("version", 0)) + 1, (held or 0) + 1)
        if version != feed["version"]:  # versions only grow, even if clocks disagree
            feed["version"] = version
            data = encode_feed(feed)
        await self._put(t, q.KIND_FEED, version, data)
        self.state.feeds[self.device_key] = {"hash": digest, "version": version}
        self.state.save()
        report.feed_pushed.append(version)
        if report.status is not None:
            report.status = q.Status(report.status.proto, report.status.flags,
                                     report.status.battery_pct, report.status.outbox,
                                     version, report.status.boot_id)
        log.info("feed v%d sent: %d B, %d conversation(s), %d action item(s), %d event(s)",
                 version, len(data), len(feed.get("conversations", [])),
                 len(feed.get("actions", [])), len(feed.get("calendar", [])))
        return True

    async def _put(self, t: Transport, kind: int, blob_id: int, data: bytes) -> None:
        if len(data) > q.MAX_BLOB:
            raise PdaError(f"blob {blob_id} is {len(data)} B, more than {q.MAX_BLOB}")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        step = q.put_payload_size(t.mtu if t.mtu >= q.MIN_MTU else q.MIN_MTU)
        for attempt in range(1, PUT_ATTEMPTS + 1):
            ready = await self._expect(t, q.encode_put_begin(kind, blob_id, len(data), crc),
                                       q.PutReady)
            if (ready.kind, ready.blob_id) != (kind, blob_id):
                raise q.ProtocolError(f"PUT_READY for {ready.kind}/{ready.blob_id}, "
                                      f"expected {kind}/{blob_id}")
            for off in range(0, len(data), step):
                await t.write(q.encode_put_data(off, data[off:off + step]))
            try:
                ok = await self._expect(t, q.encode_put_end(), q.PutOk)
            except PdaError as e:
                if e.error and e.error.code == q.ERR_BAD_CRC and attempt < PUT_ATTEMPTS:
                    log.warning("blob %d: the T5 says bad_crc, sending it again (%d/%d)",
                                blob_id, attempt + 1, PUT_ATTEMPTS)
                    continue
                raise
            if (ok.kind, ok.blob_id) != (kind, blob_id):
                raise q.ProtocolError(f"PUT_OK for {ok.kind}/{ok.blob_id}, "
                                      f"expected {kind}/{blob_id}")
            return

    # ------------------------------------------------------------ commands

    async def _expect(self, t: Transport, command: bytes, kind: type) -> Any:
        got: list[Any] = []

        def on(msg: q.Response) -> bool:
            if not isinstance(msg, kind):
                raise q.ProtocolError(f"expected {kind.__name__}, got {type(msg).__name__}")
            got.append(msg)
            return True

        await self._exchange(t, command, on)
        return got[0]

    async def _exchange(self, t: Transport, command: bytes,
                        on: Callable[[q.Response], bool]) -> None:
        """Write one command and feed its responses to `on` until it returns True.
        ERROR ends the command (PdaError); `busy` as the first answer is retried."""
        for attempt in range(BUSY_RETRIES + 1):
            await t.write(command)
            first = True
            while True:
                msg = q.decode_response(await t.receive(self.timeout))
                if isinstance(msg, q.Error):
                    if msg.code == q.ERR_BUSY and first and attempt < BUSY_RETRIES:
                        break
                    raise PdaError(str(msg), msg)
                first = False
                if on(msg):
                    return
            log.debug("T5 busy, retrying %s", q.COMMAND_NAMES.get(command[0], "?"))
            await self._sleep(BUSY_DELAY)
            t.discard_pending()
        raise AssertionError("unreachable")

    async def _bye(self, t: Transport) -> None:
        t.discard_pending()  # e.g. the PUT_END answer after an ERROR during PUT_DATA
        with contextlib.suppress(LinkLost, PdaError, q.ProtocolError,
                                 asyncio.TimeoutError, TimeoutError):  # noqa: UP041
            await self._expect(t, q.encode_bye(), q.ByeOk)


__all__ = ["Backoff", "LinkLost", "PdaState", "PdaSync", "Report"]
