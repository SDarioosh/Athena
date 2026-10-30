"""The plain text of a message's ``attributedBody``.

Newer macOS often leaves ``message.text`` empty and keeps the text only in
``attributedBody``: an ``NSAttributedString`` archived as a typedstream. Its string is the
first ``NSString`` object: the class name, a few type bytes ending in ``+`` (a C string
with a length), the length, then the UTF-8 bytes. A length below 0x80 is one byte; 0x81
is followed by a 2-byte and 0x82 by a 4-byte little-endian length.
"""

from __future__ import annotations

from typing import Optional

_CLASS = b"NSString"
_MARKER = b"+"
# the type bytes between the class name and the marker (b"\x01\x94\x84\x01" in practice)
_MAX_GAP = 16


def decode(blob: Optional[bytes]) -> Optional[str]:
    """The text in ``blob``, or None when it can't be read."""
    if not blob:
        return None
    data = bytes(blob)
    at = data.find(_CLASS)
    if at < 0:
        return None
    start = at + len(_CLASS)
    mark = data.find(_MARKER, start, start + _MAX_GAP)
    if mark < 0:
        return None
    pos = mark + 1
    if pos >= len(data):
        return None
    first = data[pos]
    if first == 0x81:
        size, pos = int.from_bytes(data[pos + 1:pos + 3], "little"), pos + 3
    elif first == 0x82:
        size, pos = int.from_bytes(data[pos + 1:pos + 5], "little"), pos + 5
    elif first < 0x80:
        size, pos = first, pos + 1
    else:
        return None
    raw = data[pos:pos + size]
    if len(raw) < size:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
