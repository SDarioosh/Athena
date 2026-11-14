"""`pda_sync.Transport` over the UI's Unix socket (`glance.sock`, Contract 1).

One `SocketTransport` is one connection. A reader task routes the UI's frames:

- DATA -> the queue `receive()` reads (a glance response, like a BLE notification);
- STATUS -> every waiter of `read_status()` / `wait_status()` (an asked-for STATUS and an
  unasked one are the same bytes: the UI's current status), and `on_status`;
- INFO -> the waiter of `read_info()`;
- LOCAL -> `on_local` (one JSON object); a LOCAL that isn't a JSON object, and unknown
  frame types, are ignored.

When the UI closes the socket (or the stream breaks), every waiter gets `LinkLost`, and
`closed` is set; the daemon then connects again. The MTU is taken as 517 (Contract 1), so
`PUT_DATA` carries 507 bytes, as on the T5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from athena_relay.drain import LinkLost

from . import frames as fr

log = logging.getLogger("athena_pda_sync.transport")

MTU = 517
REQUEST_TIMEOUT = 10.0  # INFO_REQ / STATUS_REQ answer


class SocketTransport:
    mtu = MTU

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 on_local: Callable[[dict[str, Any]], None] | None = None,
                 on_status: Callable[[bytes], None] | None = None,
                 request_timeout: float = REQUEST_TIMEOUT) -> None:
        self._reader = reader
        self._writer = writer
        self.on_local = on_local
        self.on_status = on_status
        self.request_timeout = request_timeout
        self._data: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._status_waiters: list[asyncio.Future[bytes]] = []
        self._info_waiters: list[asyncio.Future[bytes]] = []
        self.last_status: bytes | None = None
        self.closed = asyncio.Event()
        self.why = ""
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, path: Path | str, **kw: Any) -> SocketTransport:
        """Connect to the UI's socket. Raises OSError (not there, refused)."""
        reader, writer = await asyncio.open_unix_connection(str(path))
        t = cls(reader, writer, **kw)
        t.start()
        return t

    def start(self) -> None:
        self._task = asyncio.create_task(self._read_loop())

    # ------------------------------------------------------------ reading

    async def _read_loop(self) -> None:
        why = "the UI closed the socket"
        try:
            while True:
                header = await self._reader.readexactly(fr.HEADER)
                kind, length = fr.decode_header(header)
                payload = await self._reader.readexactly(length) if length else b""
                self._route(kind, payload)
        except asyncio.IncompleteReadError:
            pass
        except fr.FrameError as e:
            why = f"broken frame from the UI: {e}"
            log.warning("%s", why)
        except (OSError, ConnectionError) as e:
            why = f"socket error: {e}"
        except asyncio.CancelledError:
            why = "closed"
            raise
        finally:
            self._lost(why)

    def _route(self, kind: int, payload: bytes) -> None:
        if kind == fr.DATA:
            self._data.put_nowait(payload)
        elif kind == fr.STATUS:
            self.last_status = payload
            waiters, self._status_waiters = self._status_waiters, []
            for w in waiters:
                if not w.done():
                    w.set_result(payload)
            if self.on_status is not None:
                self.on_status(payload)
        elif kind == fr.INFO:
            waiters, self._info_waiters = self._info_waiters, []
            for w in waiters:
                if not w.done():
                    w.set_result(payload)
        elif kind == fr.LOCAL:
            msg = fr.decode_local(payload)
            if msg is None:
                log.debug("LOCAL frame that isn't a JSON object, ignored")
            elif self.on_local is not None:
                self.on_local(msg)
        else:
            log.debug("frame type 0x%02x from the UI ignored", kind)

    def _lost(self, why: str) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.why = why
        self._data.put_nowait(None)
        for w in self._status_waiters + self._info_waiters:
            if not w.done():
                w.set_exception(LinkLost(why))
        self._status_waiters, self._info_waiters = [], []
        with contextlib.suppress(Exception):
            self._writer.close()

    # ------------------------------------------------------------ writing

    async def _send(self, frame: bytes) -> None:
        if self.closed.is_set():
            raise LinkLost(self.why or "not connected")
        try:
            self._writer.write(frame)  # one write per frame: frames never interleave
            await self._writer.drain()
        except (OSError, ConnectionError, RuntimeError) as e:
            self._lost(f"socket error: {e}")
            raise LinkLost(f"socket error: {e}") from None

    async def send_local(self, message: dict[str, Any]) -> None:
        """Send one LOCAL message, its strings cut to fit (frames.fit_local). Raises
        LinkLost, or FrameError when even that can't fit (no such message is sent)."""
        await self._send(fr.encode_local(fr.fit_local(message)))

    async def _request(self, kind: int, waiters: list[asyncio.Future[bytes]],
                       timeout: float | None = None) -> bytes:
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        waiters.append(fut)
        try:
            await self._send(fr.encode(kind))
            return await asyncio.wait_for(fut, self.request_timeout if timeout is None
                                          else timeout)
        finally:
            if fut in waiters:
                waiters.remove(fut)

    # ------------------------------------------------------------ pda_sync.Transport

    async def read_info(self) -> bytes:
        return await self._request(fr.INFO_REQ, self._info_waiters)

    async def read_status(self) -> bytes:
        return await self._request(fr.STATUS_REQ, self._status_waiters)

    async def write(self, command: bytes) -> None:
        if len(command) > fr.MAX_PAYLOAD:
            raise LinkLost(f"command of {len(command)} B, at most {fr.MAX_PAYLOAD}")
        await self._send(fr.encode(fr.CTRL, command))

    async def receive(self, timeout: float) -> bytes:
        item = await asyncio.wait_for(self._data.get(), timeout)
        if item is None:
            self._data.put_nowait(None)  # keep the marker for the next reader
            raise LinkLost(self.why or "the UI closed the socket")
        return item

    def discard_pending(self) -> int:
        n = 0
        while not self._data.empty():
            if self._data.get_nowait() is None:
                self._data.put_nowait(None)
                break
            n += 1
        return n

    async def wait_status(self, timeout: float) -> bytes:
        if self.closed.is_set():
            raise LinkLost(self.why or "not connected")
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._status_waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if fut in self._status_waiters:
                self._status_waiters.remove(fut)

    # ------------------------------------------------------------ closing

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._lost("closed")

