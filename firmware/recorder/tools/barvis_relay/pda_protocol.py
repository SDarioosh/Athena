"""Codec for the T5 E-Paper Pro "glance" BLE protocol v1 (docs/t5-pda-plan.md §3).

Commands (relay -> `ctrl`) are built with the `encode_*` functions; `data` notifications are
parsed with `decode_response`. Every response type also has `encode()`, which the tests'
fake T5 uses. All integers are little-endian. Framing, `ERROR` and the shared error codes
are those of the watch protocol (docs/twatch-ble-protocol.md §5), reused from `protocol`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import (
    ERR_BAD_LENGTH,
    ERR_BAD_TIME,
    ERR_BUSY,
    ERR_IO_ERROR,
    ERR_MTU_TOO_SMALL,
    ERR_NOT_FOUND,
    ERR_UNKNOWN_CMD,
    ERROR_NAMES,
    OP_BYE,
    OP_BYE_OK,
    OP_ERROR,
    OP_TIME,
    OP_TIME_OK,
    ByeOk,
    Info,
    ProtocolError,
    _need,
)
from .protocol import Error as _WatchError

__all__ = [
    "ERR_BAD_LENGTH", "ERR_BAD_TIME", "ERR_BUSY", "ERR_IO_ERROR", "ERR_MTU_TOO_SMALL",
    "ERR_NOT_FOUND", "ERR_UNKNOWN_CMD", "ByeOk", "Info", "ProtocolError",
]

PROTO_VERSION = 1
DEVICE = "t5-pda"
BLE_NAME = "odysseus-pda"

_BASE = "-51f4-43be-86e4-1653768283cd"
SERVICE_UUID = "d0230000" + _BASE
INFO_UUID = "d0230001" + _BASE
STATUS_UUID = "d0230002" + _BASE
CTRL_UUID = "d0230003" + _BASE
DATA_UUID = "d0230004" + _BASE

MIN_MTU = 420  # §3.3: one OB_ENTRY (16 + 400 bytes) fits one notification
PUT_DATA_HEADER = 5  # opcode + offset
MAX_ATTR_VALUE = 512  # longest attribute value / write (Core spec; Android enforces it)
MAX_ACTION_JSON = 400
MAX_BLOB = 256 * 1024

# Blob kinds (§3.1)
KIND_FEED = 1
KIND_DETAIL = 2

# Commands
OP_PUT_BEGIN = 0x10
OP_PUT_DATA = 0x11
OP_PUT_END = 0x12
OP_OB_LIST = 0x20
OP_OB_ACK = 0x21
# Responses (TIME_OK, BYE_OK and ERROR as in the watch protocol)
OP_PUT_READY = 0x90
OP_PUT_OK = 0x91
OP_OB_ENTRY = 0xA0
OP_OB_END = 0xA1
OP_OB_ACKED = 0xA2

# ERROR codes added by this service (§3.3); 1-7 are the watch protocol's
ERR_BAD_CRC = 8
ERR_TOO_LARGE = 9
ERR_BAD_STATE = 10
PDA_ERROR_NAMES = {**ERROR_NAMES, ERR_BAD_CRC: "bad_crc", ERR_TOO_LARGE: "too_large",
                   ERR_BAD_STATE: "bad_state"}
COMMAND_NAMES = {OP_TIME: "TIME", OP_BYE: "BYE", OP_PUT_BEGIN: "PUT_BEGIN",
                 OP_PUT_DATA: "PUT_DATA", OP_PUT_END: "PUT_END", OP_OB_LIST: "OB_LIST",
                 OP_OB_ACK: "OB_ACK"}

# status flags (§3.2)
ST_TIME_VALID = 0x01
ST_CHARGING = 0x02
ST_USB_POWER = 0x04
ST_PAIRING_WINDOW = 0x08
ST_USER_ACTIVE = 0x10

# OB_ACK results (§3.3)
RESULT_DONE = 0
RESULT_REJECTED = 1

OB_LIST_MAX = 16  # entries per OB_LIST; the relay pages with after_id

# Scan response (§3.4 step 6): 128-bit service data for SERVICE_UUID, flags byte
ADV_OUTBOX_WAITING = 0x01


# ------------------------------------------------------------------ commands


def encode_time(epoch_ms: int, tz_offset_min: int) -> bytes:
    return struct.pack("<B3xQh", OP_TIME, epoch_ms, tz_offset_min)


def encode_put_begin(kind: int, blob_id: int, total: int, crc32: int) -> bytes:
    return struct.pack("<BBHIII", OP_PUT_BEGIN, kind, 0, blob_id, total, crc32)


def encode_put_data(offset: int, payload: bytes) -> bytes:
    return struct.pack("<BI", OP_PUT_DATA, offset) + payload


def encode_put_end() -> bytes:
    return bytes([OP_PUT_END])


def encode_ob_list(after_id: int, max_entries: int = OB_LIST_MAX) -> bytes:
    return struct.pack("<BIH", OP_OB_LIST, after_id, max_entries)


def encode_ob_ack(action_id: int, result: int) -> bytes:
    return struct.pack("<BIB", OP_OB_ACK, action_id, result)


def encode_bye() -> bytes:
    return bytes([OP_BYE])


def put_payload_size(mtu: int) -> int:
    """Bytes of blob per PUT_DATA write: the frame is at most MTU - 3 (ATT header) and at
    most 512 (an attribute value's limit), minus 5 (opcode, offset). 507 at MTU 517."""
    return min(mtu - 3, MAX_ATTR_VALUE) - PUT_DATA_HEADER


# ------------------------------------------------------------------ responses


@dataclass(frozen=True)
class TimeOk:
    LEN = 4

    def encode(self) -> bytes:
        return struct.pack("<BBH", OP_TIME_OK, 0, 0)

    @classmethod
    def decode(cls, b: bytes) -> TimeOk:
        _need(b, cls.LEN, "TIME_OK")
        return cls()


class _BlobAnswer:
    """PUT_READY and PUT_OK: `op` `kind:u8` `00 00` `blob_id:u32` (8 bytes)."""

    LEN = 8
    _OP = 0
    kind: int
    blob_id: int

    def encode(self) -> bytes:
        return struct.pack("<BBHI", self._OP, self.kind, 0, self.blob_id)

    @classmethod
    def decode(cls, b: bytes):
        _need(b, cls.LEN, cls.__name__)
        _, kind, _, blob_id = struct.unpack_from("<BBHI", b)
        return cls(kind, blob_id)


@dataclass(frozen=True)
class PutReady(_BlobAnswer):
    kind: int
    blob_id: int

    _OP = OP_PUT_READY


@dataclass(frozen=True)
class PutOk(_BlobAnswer):
    kind: int
    blob_id: int

    _OP = OP_PUT_OK


@dataclass(frozen=True)
class ObEntry:
    action_id: int
    created_ms: int
    json: bytes

    HEADER = 16

    def encode(self) -> bytes:
        return struct.pack("<BBHIQ", OP_OB_ENTRY, 0, len(self.json), self.action_id,
                           self.created_ms) + self.json

    @classmethod
    def decode(cls, b: bytes) -> ObEntry:
        _need(b, cls.HEADER, "OB_ENTRY")
        _, _, length, action_id, created = struct.unpack_from("<BBHIQ", b)
        if len(b) < cls.HEADER + length:
            raise ProtocolError(f"OB_ENTRY: {len(b) - cls.HEADER} JSON bytes, header says "
                                f"{length}")
        return cls(action_id, created, bytes(b[cls.HEADER:cls.HEADER + length]))


@dataclass(frozen=True)
class ObEnd:
    count: int
    remaining: int

    LEN = 8

    def encode(self) -> bytes:
        return struct.pack("<BBHI", OP_OB_END, 0, self.count, self.remaining)

    @classmethod
    def decode(cls, b: bytes) -> ObEnd:
        _need(b, cls.LEN, "OB_END")
        _, _, count, remaining = struct.unpack_from("<BBHI", b)
        return cls(count, remaining)


@dataclass(frozen=True)
class ObAcked:
    result: int
    action_id: int

    LEN = 8

    def encode(self) -> bytes:
        return struct.pack("<BBHI", OP_OB_ACKED, self.result, 0, self.action_id)

    @classmethod
    def decode(cls, b: bytes) -> ObAcked:
        _need(b, cls.LEN, "OB_ACKED")
        _, result, _, action_id = struct.unpack_from("<BBHI", b)
        return cls(result, action_id)


@dataclass(frozen=True)
class Error(_WatchError):
    """ERROR as in the watch protocol, with this service's command and error names."""

    @property
    def name(self) -> str:
        return PDA_ERROR_NAMES.get(self.code, f"error_{self.code}")

    def __str__(self) -> str:
        cmd = COMMAND_NAMES.get(self.cmd, f"0x{self.cmd:02x}")
        return f"ERROR {self.name} on {cmd} (arg {self.arg})"


Response = TimeOk | PutReady | PutOk | ObEntry | ObEnd | ObAcked | ByeOk | Error

_DECODERS: dict[int, type] = {
    OP_TIME_OK: TimeOk, OP_PUT_READY: PutReady, OP_PUT_OK: PutOk, OP_OB_ENTRY: ObEntry,
    OP_OB_END: ObEnd, OP_OB_ACKED: ObAcked, OP_BYE_OK: ByeOk, OP_ERROR: Error,
}


def decode_response(b: bytes) -> Response:
    if not b:
        raise ProtocolError("empty notification")
    cls = _DECODERS.get(b[0])
    if cls is None:
        raise ProtocolError(f"unknown response opcode 0x{b[0]:02x}")
    return cls.decode(bytes(b))


# ------------------------------------------------------------------ status


@dataclass(frozen=True)
class Status:
    proto: int
    flags: int
    battery_pct: int  # -1 unknown
    outbox: int
    feed_version: int  # 0 if no feed
    boot_id: int

    LEN = 16
    _FMT = "<BBbBHHII"

    @property
    def user_active(self) -> bool:
        return bool(self.flags & ST_USER_ACTIVE)

    def encode(self) -> bytes:
        return struct.pack(self._FMT, self.proto, self.flags, self.battery_pct, 0,
                           self.outbox, 0, self.feed_version, self.boot_id)

    @classmethod
    def decode(cls, b: bytes) -> Status:
        if len(b) < cls.LEN:
            raise ProtocolError(f"status: {len(b)} bytes, expected {cls.LEN}")
        proto, flags, pct, _, outbox, _, version, boot = struct.unpack_from(cls._FMT, b)
        return cls(proto, flags, pct, outbox, version, boot)

    def describe(self) -> str:
        names = [("time-valid", ST_TIME_VALID), ("charging", ST_CHARGING),
                 ("usb", ST_USB_POWER), ("pairing-window", ST_PAIRING_WINDOW),
                 ("active", ST_USER_ACTIVE)]
        flags = ",".join(n for n, bit in names if self.flags & bit) or "-"
        batt = f"{self.battery_pct}%" if self.battery_pct >= 0 else "?%"
        return (f"flags={flags} battery={batt} outbox={self.outbox} "
                f"feed_version={self.feed_version} boot={self.boot_id:08x}")


def outbox_waiting(service_data: bytes | None) -> bool:
    """Whether the scan response's service data says the outbox has entries (§3.4)."""
    return bool(service_data) and bool(service_data[0] & ADV_OUTBOX_WAITING)
