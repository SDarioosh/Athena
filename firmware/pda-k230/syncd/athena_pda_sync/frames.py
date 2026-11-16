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


def _local_json(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()


def encode_local(message: dict[str, Any]) -> bytes:
    """A LOCAL frame: compact UTF-8 JSON (not ASCII-escaped, so more fits in 512 B).
    Raises FrameError when it is over 512 B (`fit_local` first makes sure it isn't)."""
    return encode(LOCAL, _local_json(message))


# The UI's buffers for the strings syncd sends (bytes, without the NUL; app_state.h,
# local_msg.h): a longer one would reach the UI empty. `url` (the tailnet login link) is
# never cut, a cut link is no link: one too long goes as null.
FIELD_BYTES = {"hub": 127, "msg": 127, "name": 127, "url": 255}
ELLIPSIS = "\u2026"


def cut_utf8(text: str, max_bytes: int) -> str:
    """text in at most max_bytes of UTF-8, cut at a character with "…" when it is longer."""
    if len(text.encode()) <= max_bytes:
        return text
    room = max_bytes - len(ELLIPSIS.encode())
    out = text.encode()[:max(room, 0)].decode("utf-8", "ignore")
    return out + ELLIPSIS if room >= 0 else ""


def fit_local(message: dict[str, Any]) -> dict[str, Any]:
    """The message with its strings cut to the UI's buffers (FIELD_BYTES) and, if the JSON
    is still over 512 B (escapes, unknown fields), its longest strings cut further, `t`
    and `url` never. Encodes with encode_local without a FrameError."""
    out: dict[str, Any] = {}
    for k, v in message.items():
        if isinstance(v, str) and k in FIELD_BYTES:
            if k == "url":
                v = v if len(v.encode()) <= FIELD_BYTES[k] else None
            else:
                v = cut_utf8(v, FIELD_BYTES[k])
        out[k] = v
    while len(_local_json(out)) > MAX_PAYLOAD:
        cuttable = [k for k, v in out.items() if isinstance(v, str) and v and k not in ("t", "url")]
        if not cuttable:
            if isinstance(out.get("url"), str):
                out["url"] = None
                continue
            raise FrameError(f"LOCAL {message.get('t')!r} can't be made to fit")
        k = max(cuttable, key=lambda key: len(out[key].encode()))
        size = len(out[k].encode())
        out[k] = cut_utf8(out[k], max(size - max(len(_local_json(out)) - MAX_PAYLOAD, 8), 0))
    return out


def decode_local(payload: bytes) -> dict[str, Any] | None:
    """The JSON object of a LOCAL frame, or None if it isn't one (ignored, like unknown
    types)."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None
