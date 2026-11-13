"""Frames of `glance.sock` (docs/k230-pda-plan.md, Contract 1): `type:u8` `len:u16 LE`
`payload`, `len` at most 512.

| Type | Direction | Payload |
|---|---|---|
| CTRL 0x01 | syncd -> UI | one glance command (T5 plan §3.3), bytes as on BLE |
| DATA 0x02 | UI -> syncd | one glance response |
| STATUS_REQ 0x03 | syncd -> UI | empty |
| STATUS 0x04 | UI -> syncd | the 16-byte status (§3.2), asked for or unasked |
| INFO_REQ 0x05 | syncd -> UI | empty |
| INFO 0x06 | UI -> syncd | `proto=1;device=k230-pda;fw=...;board=t_display_k230` |
| LOCAL 0x10 | both | one UTF-8 JSON object |
"""

from __future__ import annotations

import json
import struct
from typing import Any

CTRL = 0x01
DATA = 0x02
STATUS_REQ = 0x03
STATUS = 0x04
INFO_REQ = 0x05
INFO = 0x06
LOCAL = 0x10

HEADER = 3
MAX_PAYLOAD = 512
NAMES = {CTRL: "CTRL", DATA: "DATA", STATUS_REQ: "STATUS_REQ", STATUS: "STATUS",
         INFO_REQ: "INFO_REQ", INFO: "INFO", LOCAL: "LOCAL"}


class FrameError(Exception):
    """A frame that can't be sent or read (too long, a broken header)."""


def encode(kind: int, payload: bytes = b"") -> bytes:
    if not 0 <= kind <= 0xFF:
        raise FrameError(f"frame type {kind} out of range")
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"{NAMES.get(kind, kind)} payload is {len(payload)} B, "
                         f"at most {MAX_PAYLOAD}")
    return struct.pack("<BH", kind, len(payload)) + payload


def decode_header(header: bytes) -> tuple[int, int]:
    """(type, len) of a 3-byte header. A `len` over 512 is a broken stream."""
    if len(header) != HEADER:
        raise FrameError(f"header of {len(header)} bytes")
    kind, length = struct.unpack("<BH", header)
    if length > MAX_PAYLOAD:
        raise FrameError(f"{NAMES.get(kind, hex(kind))} frame says {length} B, "
                         f"at most {MAX_PAYLOAD}")
    return kind, length


def encode_local(message: dict[str, Any]) -> bytes:
    """A LOCAL frame: compact UTF-8 JSON (not ASCII-escaped, so more fits in 512 B)."""
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    return encode(LOCAL, raw)


def decode_local(payload: bytes) -> dict[str, Any] | None:
    """The JSON object of a LOCAL frame, or None if it isn't one (ignored, like unknown
    types)."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None
