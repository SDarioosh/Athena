"""Codec for the T-Watch Ultra BLE protocol v1 (docs/twatch-ble-protocol.md §3-§5, §8).

Commands (relay -> `ctrl`) are built with the `encode_*` functions; `data` notifications are
parsed with `decode_response`. Every response type also has `encode()`, which the tests'
fake watch uses. All integers are little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PROTO_VERSION = 1

_BASE = "-61c0-4fda-8b7b-26b08e916438"
SERVICE_UUID = "f8060000" + _BASE
INFO_UUID = "f8060001" + _BASE
STATUS_UUID = "f8060002" + _BASE
CTRL_UUID = "f8060003" + _BASE
DATA_UUID = "f8060004" + _BASE

MIN_MTU = 64
DATA_HEADER = 9  # opcode + chunk_id + offset

# Commands
OP_LIST = 0x01
OP_READ = 0x02
OP_ACK = 0x03
OP_TIME = 0x04
OP_BYE = 0x05
# Responses
OP_ENTRY = 0x81
OP_DATA = 0x82
OP_READ_END = 0x83
OP_ACKED = 0x84
OP_LIST_END = 0x85
OP_TIME_OK = 0x86
OP_BYE_OK = 0x87
OP_ERROR = 0xEE

# ERROR codes (§5.6)
ERR_BAD_LENGTH = 1
ERR_UNKNOWN_CMD = 2
ERR_NOT_FOUND = 3
ERR_BUSY = 4
ERR_MTU_TOO_SMALL = 5
ERR_IO_ERROR = 6
ERR_BAD_TIME = 7
ERROR_NAMES = {
    ERR_BAD_LENGTH: "bad_length",
    ERR_UNKNOWN_CMD: "unknown_cmd",
    ERR_NOT_FOUND: "not_found",
    ERR_BUSY: "busy",
    ERR_MTU_TOO_SMALL: "mtu_too_small",
    ERR_IO_ERROR: "io_error",
    ERR_BAD_TIME: "bad_time",
}
COMMAND_NAMES = {OP_LIST: "LIST", OP_READ: "READ", OP_ACK: "ACK", OP_TIME: "TIME", OP_BYE: "BYE"}

# ENTRY flags (§4.3)
FLAG_LAST = 0x01
FLAG_TIME_UNSET = 0x02

# status flags (§4.2)
ST_RECORDING = 0x01
ST_MUTED = 0x02
ST_TIME_VALID = 0x04
ST_CHARGING = 0x08
ST_USB_POWER = 0x10
ST_SD_OK = 0x20
ST_SYNC_WINDOW = 0x40

LIST_MAX = 32  # the watch caps LIST at 32 entries


class ProtocolError(Exception):
    """A notification or value that doesn't follow the protocol."""


# ------------------------------------------------------------------ commands


def encode_list(after_id: int, max_entries: int = LIST_MAX) -> bytes:
    return struct.pack("<BIH", OP_LIST, after_id, max_entries)


def encode_read(chunk_id: int, offset: int = 0, length: int = 0) -> bytes:
    return struct.pack("<BIII", OP_READ, chunk_id, offset, length)


def encode_ack(chunk_id: int) -> bytes:
    return struct.pack("<BI", OP_ACK, chunk_id)


def encode_time(epoch_ms: int) -> bytes:
    return struct.pack("<B3xQ", OP_TIME, epoch_ms)


def encode_bye() -> bytes:
    return bytes([OP_BYE])


# ------------------------------------------------------------------ responses


@dataclass(frozen=True)
class Entry:
    chunk_id: int
    session_id: bytes
    sequence: int
    start_ms: int
    duration_ms: int
    size: int
    crc32: int
    flags: int = 0

    LEN = 40
    _FMT = "<BBHI8sIQIII"

    @property
    def last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    @property
    def time_unset(self) -> bool:
        return bool(self.flags & FLAG_TIME_UNSET)

    @property
    def session_hex(self) -> str:
        return self.session_id.hex()

    def encode(self) -> bytes:
        return struct.pack(self._FMT, OP_ENTRY, self.flags, 0, self.chunk_id, self.session_id,
                           self.sequence, self.start_ms, self.duration_ms, self.size, self.crc32)

    @classmethod
    def decode(cls, b: bytes) -> Entry:
        _need(b, cls.LEN, "ENTRY")
        _, flags, _, cid, sid, seq, start, dur, size, crc = struct.unpack_from(cls._FMT, b)
        return cls(cid, sid, seq, start, dur, size, crc, flags)


@dataclass(frozen=True)
class DataFrame:
    chunk_id: int
    offset: int
    payload: bytes

    def encode(self) -> bytes:
        return struct.pack("<BII", OP_DATA, self.chunk_id, self.offset) + self.payload

    @classmethod
    def decode(cls, b: bytes) -> DataFrame:
        _need(b, DATA_HEADER, "DATA")
        _, cid, off = struct.unpack_from("<BII", b)
        return cls(cid, off, bytes(b[DATA_HEADER:]))


@dataclass(frozen=True)
class ReadEnd:
    status: int
    chunk_id: int
    total_bytes: int
    crc32: int

    LEN = 16

    def encode(self) -> bytes:
        return struct.pack("<BBHIII", OP_READ_END, self.status, 0, self.chunk_id,
                           self.total_bytes, self.crc32)

    @classmethod
    def decode(cls, b: bytes) -> ReadEnd:
        _need(b, cls.LEN, "READ_END")
        _, status, _, cid, total, crc = struct.unpack_from("<BBHIII", b)
        return cls(status, cid, total, crc)


@dataclass(frozen=True)
class Acked:
    result: int  # 0 deleted, 1 was not queued (both are success)
    chunk_id: int

    LEN = 8

    def encode(self) -> bytes:
        return struct.pack("<BBHI", OP_ACKED, self.result, 0, self.chunk_id)

    @classmethod
    def decode(cls, b: bytes) -> Acked:
        _need(b, cls.LEN, "ACKED")
        _, result, _, cid = struct.unpack_from("<BBHI", b)
        return cls(result, cid)


@dataclass(frozen=True)
class ListEnd:
    count: int
    remaining: int

    LEN = 8

    def encode(self) -> bytes:
        return struct.pack("<BBHI", OP_LIST_END, 0, self.count, self.remaining)

    @classmethod
    def decode(cls, b: bytes) -> ListEnd:
        _need(b, cls.LEN, "LIST_END")
        _, _, count, remaining = struct.unpack_from("<BBHI", b)
        return cls(count, remaining)


@dataclass(frozen=True)
class TimeOk:
    fixed: int

    LEN = 4

    def encode(self) -> bytes:
        return struct.pack("<BBH", OP_TIME_OK, 0, self.fixed)

    @classmethod
    def decode(cls, b: bytes) -> TimeOk:
        _need(b, cls.LEN, "TIME_OK")
        return cls(struct.unpack_from("<BBH", b)[2])


@dataclass(frozen=True)
class ByeOk:
    def encode(self) -> bytes:
        return bytes([OP_BYE_OK])

    @classmethod
    def decode(cls, b: bytes) -> ByeOk:
        return cls()


@dataclass(frozen=True)
class Error:
    cmd: int
    code: int
    arg: int = 0

    LEN = 8

    @property
    def name(self) -> str:
        return ERROR_NAMES.get(self.code, f"error_{self.code}")

    def __str__(self) -> str:
        cmd = COMMAND_NAMES.get(self.cmd, f"0x{self.cmd:02x}")
        return f"ERROR {self.name} on {cmd} (arg {self.arg})"

    def encode(self) -> bytes:
        return struct.pack("<BBBBI", OP_ERROR, self.cmd, self.code, 0, self.arg)

    @classmethod
    def decode(cls, b: bytes) -> Error:
        _need(b, cls.LEN, "ERROR")
        _, cmd, code, _, arg = struct.unpack_from("<BBBBI", b)
        return cls(cmd, code, arg)


Response = Entry | DataFrame | ReadEnd | Acked | ListEnd | TimeOk | ByeOk | Error

_DECODERS: dict[int, type] = {
    OP_ENTRY: Entry, OP_DATA: DataFrame, OP_READ_END: ReadEnd, OP_ACKED: Acked,
    OP_LIST_END: ListEnd, OP_TIME_OK: TimeOk, OP_BYE_OK: ByeOk, OP_ERROR: Error,
}


def decode_response(b: bytes) -> Response:
    if not b:
        raise ProtocolError("empty notification")
    cls = _DECODERS.get(b[0])
    if cls is None:
        raise ProtocolError(f"unknown response opcode 0x{b[0]:02x}")
    return cls.decode(bytes(b))


def _need(b: bytes, n: int, what: str) -> None:
    # Responses may grow at the end in later compatible versions (§9), so longer is fine.
    if len(b) < n:
        raise ProtocolError(f"{what}: {len(b)} bytes, expected {n}")


# ------------------------------------------------------------------ info / status


@dataclass(frozen=True)
class Info:
    proto: int
    device: str
    fw: str = ""
    board: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: bytes | str) -> Info:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes | bytearray) else raw
        kv: dict[str, str] = {}
        for part in text.strip().strip("\x00").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k.strip()] = v.strip()
        try:
            proto = int(kv.pop("proto"))
        except (KeyError, ValueError) as e:
            raise ProtocolError(f"info has no valid proto: {text!r}") from e
        device = kv.pop("device", "")
        if not device:
            raise ProtocolError(f"info has no device: {text!r}")
        return cls(proto, device, kv.pop("fw", ""), kv.pop("board", ""), kv)

    def encode(self) -> bytes:
        parts = [f"proto={self.proto}", f"device={self.device}"]
        if self.fw:
            parts.append(f"fw={self.fw}")
        if self.board:
            parts.append(f"board={self.board}")
        parts += [f"{k}={v}" for k, v in self.extra.items()]
        return ";".join(parts).encode()


@dataclass(frozen=True)
class Status:
    proto: int
    flags: int
    battery_pct: int  # -1 unknown
    battery_mv: int  # 0 unknown
    queued_chunks: int
    queued_bytes: int
    queued_ms: int
    clock_s: int  # 0 if not valid
    boot_id: int

    LEN = 24
    _FMT = "<BBbBHHIIII"

    def encode(self) -> bytes:
        return struct.pack(self._FMT, self.proto, self.flags, self.battery_pct, 0,
                           self.battery_mv, self.queued_chunks, self.queued_bytes,
                           self.queued_ms, self.clock_s, self.boot_id)

    @classmethod
    def decode(cls, b: bytes) -> Status:
        if len(b) < cls.LEN:
            raise ProtocolError(f"status: {len(b)} bytes, expected {cls.LEN}")
        proto, flags, pct, _, mv, n, nbytes, ms, clock, boot = struct.unpack_from(cls._FMT, b)
        return cls(proto, flags, pct, mv, n, nbytes, ms, clock, boot)

    def describe(self) -> str:
        names = [("recording", ST_RECORDING), ("muted", ST_MUTED), ("time-valid", ST_TIME_VALID),
                 ("charging", ST_CHARGING), ("usb", ST_USB_POWER), ("sd-ok", ST_SD_OK),
                 ("sync-window", ST_SYNC_WINDOW)]
        flags = ",".join(n for n, bit in names if self.flags & bit) or "-"
        batt = f"{self.battery_pct}%" if self.battery_pct >= 0 else "?%"
        if self.battery_mv:
            batt += f" {self.battery_mv} mV"
        clock = iso_ms(self.clock_s * 1000) if self.clock_s else "not set"
        return (f"flags={flags} battery={batt} queue={self.queued_chunks} chunks/"
                f"{self.queued_bytes} B/{self.queued_ms / 1000:.0f} s clock={clock} "
                f"boot={self.boot_id:08x}")


# ------------------------------------------------------------------ helpers

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # noqa: UP017 (Python 3.10 has no UTC)


def iso_ms(epoch_ms: int) -> str:
    """UTC epoch milliseconds -> ISO-8601 with milliseconds, e.g. 2026-10-19T12:34:56.789Z."""
    dt = _EPOCH + timedelta(milliseconds=epoch_ms)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{epoch_ms % 1000:03d}Z"
