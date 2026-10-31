"""What a thread's summary is told, and which threads get one (docs/messages.md,
"Summaries")."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..domain import Segment
from .repo import label


def context(text: dict[str, Any], segments: list[Segment], timezone: str) -> str:
    """The lines put before the transcript: whose messages these are and when they
    start, so the model reads "tomorrow" from the right day and doesn't look for
    speech."""
    start = datetime.fromtimestamp(segments[0].started_at, ZoneInfo(timezone))
    group = f' named "{text["name"]}"' if text["group"] and text["name"] else ""
    return (f"{label(text)}. These are text messages ({text['source']}), one per line, "
            f"in a {'group chat' + group if text['group'] else 'one-to-one chat'}; the "
            f"number in brackets is seconds since the first message, sent "
            f"{start.strftime('%A %Y-%m-%d %H:%M')} ({timezone}). Short replies and "
            "emoji are normal in texts; promises and plans count like spoken ones.")


def too_short(segments: list[Segment], settings: Settings) -> bool:
    """Too few words for a summary: the thread is stored and searchable without one."""
    return sum(len(s.text.split()) for s in segments) < settings.text_summary_min_words


def archive_before(settings: Settings, now: float) -> float | None:
    """Threads that ended before this get no summary unless asked (None: all do)."""
    days = settings.text_summary_days
    return now - days * 86400 if days > 0 else None
