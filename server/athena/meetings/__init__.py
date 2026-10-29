"""Online meetings: the bot launcher, ingest, transcription and people of a meeting.

- ``bot/``: the meeting bot launcher (``bot.launcher``, with the meeting link parsing)
  and Athena's Zoom sign-in (``bot.zoom_signin``).
- ``ingest/``: a meeting's chat, topic and participants (``ingest.chat``).
- ``transcribe/``: per-track transcription and bleed (``transcribe.tracks``) and live
  transcription while a meeting records (``transcribe.live``).
- ``people/``: display names (``people.names``) and voices learned from meetings
  (``people.voices``).

``athena.meetings`` was the launcher module before it became this package, so its
names (``Launcher``, ``MeetingError``, ``parse_zoom_link``, ...) are still read here.
They load the launcher on first use, not on import: a server that never gets a
meeting request never imports it, and importing ``people`` or ``transcribe`` doesn't
either. To monkeypatch the launcher, patch ``athena.meetings.bot.launcher``.
"""

from __future__ import annotations

import importlib
from typing import Any

_SUBPACKAGES = ("bot", "ingest", "people", "transcribe")


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in _SUBPACKAGES:  # ``from athena.meetings import people`` loads no launcher
        return importlib.import_module("." + name, __name__)
    launcher = importlib.import_module(".bot.launcher", __name__)
    try:
        return getattr(launcher, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
