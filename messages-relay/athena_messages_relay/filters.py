"""Messages that never leave the Mac: one-time codes and automated short-code senders.

Kept narrow on purpose: "the door code is 4321" is the owner's information and is sent;
"Your verification code is 482913" is not."""

from __future__ import annotations

import re
from typing import Optional

# a code-ish token: 4–8 digits (also "123-456"), or 6–8 letters and digits with a digit
_TOKEN = re.compile(r"(?<!\d)\d{4,8}(?!\d)|(?<!\d)\d{3}[- ]\d{3}(?!\d)"
                    r"|\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,8}\b")
_STRONG = re.compile(
    r"\b(?:verification|security|login|log-in|sign[- ]?in|authentication|auth|"
    r"confirmation|access|one[- ]time|2fa|two[- ]factor|activation)\s+(?:code|pin|passcode)\b"
    r"|\bone[- ]time\s+pass(?:word|code)\b"
    r"|\byour\s+(?:\w+\s+){0,3}(?:code|pin|passcode)\s+(?:is|:)"
    r"|\bis\s+your\s+(?:\w+\s+){0,3}(?:code|pin|passcode)\b"
    r"|\b(?:do\s+not|don['’]t|never)\s+share\s+(?:this|the|your)\s+(?:code|pin)\b"
    r"|\bOTP\b",
    re.IGNORECASE)
# 5–6 digit senders are short codes: banks, deliveries, sign-in codes, never people
_SHORT_CODE = re.compile(r"^\d{5,6}$")


def is_short_code(handle: Optional[str]) -> bool:
    return bool(handle) and bool(_SHORT_CODE.match(handle.strip()))


def is_one_time_code(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(_STRONG.search(text)) and bool(_TOKEN.search(text))


def keep(text: Optional[str], handle: Optional[str], from_me: bool) -> bool:
    """Whether a message may be sent to the server."""
    if not from_me and is_short_code(handle):
        return False
    return not is_one_time_code(text)
