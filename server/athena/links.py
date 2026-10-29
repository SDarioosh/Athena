"""Signed transcript links, and the labels shown next to a title or line.

Links look like ``<base>/view/<conversation_id>?sig=<hmac>#t=<seconds>`` so they can be
opened from a phone browser without the API bearer token. The signature is an HMAC of
the conversation ID keyed from ``CONVO_INGEST_TOKEN``; rotating the token revokes all
links. Serve the API on loopback/Tailscale only. The page a link opens is rendered by
``views.py`` (its names are still importable from here).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .config import Settings
from .timeutil import local_time as _local_time


def _key(settings: Settings) -> bytes:
    return hmac.new(settings.ingest_token.encode(), b"convo-transcript-links",
                    hashlib.sha256).digest()


def sign(settings: Settings, conv_id: str) -> str:
    return hmac.new(_key(settings), conv_id.encode(), hashlib.sha256).hexdigest()[:32]


def verify(settings: Settings, conv_id: str, sig: str) -> bool:
    return hmac.compare_digest(sign(settings, conv_id), sig or "")


def transcript_link(settings: Settings, conv_id: str, offset: float | None = None) -> str:
    url = f"{settings.base_url}/view/{conv_id}?sig={sign(settings, conv_id)}"
    return url + (f"#t={max(0, int(offset))}" if offset is not None else "")


def local_time(settings: Settings, ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    return _local_time(ts, settings.timezone, fmt)


def meeting_label(meeting: dict[str, Any] | None) -> str:
    """" (Online meeting: <topic>)" after a title, " (Online meeting)" without a topic; ""
    for anything else (MCP 0.17.0; 0.16.0 said "Zoom meeting"). It names no platform:
    meetings may come from others than Zoom later."""
    if not meeting:
        return ""
    return f" (Online meeting: {meeting['topic']})" if meeting.get("topic") else " (Online meeting)"


def speaker_label(segment: Any) -> str:
    """A line's speaker as shown: "Dana (chat)" for a meeting chat message."""
    kind = segment.get("kind") if isinstance(segment, dict) else getattr(segment, "kind", None)
    name = segment["speaker"] if isinstance(segment, dict) else segment.speaker
    return f"{name} (chat)" if kind == "chat" else name


def written_label(written: dict[str, Any] | None) -> str:
    """" (written by hand: <hub link>)" after a title or fact; "" for spoken ones."""
    return f" (written by hand: {written['link']})" if written else ""


# The /view page moved to views.py; its names are still read from here.
_MOVED_TO_VIEWS = frozenset({"SECTIONS", "render_conversation", "title_of", "when_of",
                             "_no_summary", "_written", "_meeting", "_PAGE", "_JUMP_SCRIPT"})


def __getattr__(name: str) -> Any:
    if name in _MOVED_TO_VIEWS:
        from . import views

        return getattr(views, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
