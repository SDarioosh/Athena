"""A conversation as a file the owner can send to other people.

``GET /v1/conversations/{id}/export?format=md|html&include=...`` answers with the file
(``Content-Disposition: attachment``); the hub's Share button gets it through the MCP
server's relay route ``GET /conversations/{id}/export``.

A shared copy is the owner's choice to send: it holds what they picked and nothing that
reaches back into their network. No signed transcript links, no link to the hub, no ids.
Lines erased with ``erase_fact`` stay ``[forgotten]``, and a summary waiting for its
rewrite after a forget stays hidden, as everywhere else.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from .config import Settings
from .links import (
    SECTIONS,
    _no_summary,
    local_time,
    meeting_label,
    render_conversation,
    speaker_label,
    title_of,
    when_of,
)

FORMATS = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}
# Facts are left out unless asked for: they are notes about people, not what was said.
DEFAULT_INCLUDE = ("summary", "action_items", "transcript")
_UNSAFE = re.compile(r"[^\w\- ]+", re.UNICODE)


def parse_include(raw: str | None) -> tuple[str, ...]:
    """``"summary,transcript"`` -> the sections in page order. Empty means the default;
    an unknown name is a ValueError (the API answers 400)."""
    if raw is None or not raw.strip():
        return DEFAULT_INCLUDE
    asked = {p.strip().lower().replace("-", "_") for p in raw.split(",") if p.strip()}
    unknown = asked - set(SECTIONS)
    if unknown:
        raise ValueError(f"include takes {', '.join(SECTIONS)}; not {', '.join(sorted(unknown))}")
    return tuple(s for s in SECTIONS if s in asked)


def filename(settings: Settings, conv: dict[str, Any], fmt: str) -> str:
    """"2025-10-09 Planning the trip.md": the local date, then the title made safe for a
    file name (letters of any script, digits, spaces and dashes; at most 60 characters)."""
    day = local_time(settings, conv["started_at"], "%Y-%m-%d") or ""
    title = " ".join(_UNSAFE.sub(" ", title_of(conv)).split())[:60].strip()
    return f"{day} {title or 'Conversation'}.{fmt}".strip()


def content_disposition(name: str) -> str:
    """``attachment`` with an ASCII fallback name and the UTF-8 one (RFC 6266)."""
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def render(settings: Settings, conv: dict[str, Any], segments: list[Any], fmt: str,
           include: Iterable[str] = DEFAULT_INCLUDE) -> str:
    include = tuple(include)
    # an item the owner dismissed was never theirs to do: not worth sending
    conv = {**conv, "action_items": [a for a in conv.get("action_items") or []
                                     if a.get("status") != "dismissed"]}
    if fmt == "html":
        return render_conversation(settings, conv, segments, include=include, shared=True)
    if fmt == "md":
        return render_markdown(settings, conv, segments, include)
    raise ValueError(f"format must be one of {', '.join(FORMATS)}")


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def render_markdown(settings: Settings, conv: dict[str, Any], segments: list[Any],
                    include: Iterable[str] = DEFAULT_INCLUDE) -> str:
    include = set(include)
    hidden = bool(conv.get("forget_pending"))
    head = [when_of(settings, conv)]
    if conv.get("people"):
        head.append(f"People: {', '.join(conv['people'])}")
    meeting = conv.get("meeting")
    if meeting:
        line = meeting_label(meeting).strip(" ()")
        names = [p["name"] for p in meeting.get("participants") or []]
        head.append(line + (f" · Participants: {', '.join(names)}" if names else ""))
    if conv.get("written"):
        head.append("Written by hand")
    # two trailing spaces: one line each in Markdown
    out = [f"# {_one_line(title_of(conv))}", "  \n".join(head)]

    if "summary" in include:
        summary = _no_summary(conv) if hidden else conv.get("summary") or _no_summary(conv)
        out += ["## Summary", _one_line(summary)]
        points = [] if hidden else conv.get("key_points") or []
        if points:
            out += ["### Key points", "\n".join(f"- {_one_line(k)}" for k in points)]
    if "action_items" in include and conv.get("action_items"):
        rows = []
        for a in conv["action_items"]:
            extra = ", ".join(_one_line(x) for x in (a.get("owner"), a.get("due") and
                                                     f"due {a['due']}") if x)
            rows.append(f"- [{'x' if a.get('status') == 'done' else ' '}] "
                        f"{_one_line(a['text'])}" + (f" ({extra})" if extra else ""))
        out += ["## Action items", "\n".join(rows)]
    if "facts" in include and conv.get("facts"):
        out += ["## Facts", "\n".join(f"- {_one_line(f['fact'])}" for f in conv["facts"])]
    if "transcript" in include:
        out.append("## Transcript")
        lines = [f"**{local_time(settings, s.started_at, '%H:%M:%S')} "
                 f"{_one_line(speaker_label(s))}:** {_one_line(s.text)}" for s in segments]
        if lines:
            out.append("\n\n".join(lines))
        elif pruned := conv.get("transcript_pruned_at"):
            out.append(f"The transcript was removed on "
                       f"{local_time(settings, pruned, '%Y-%m-%d')} by the retention setting.")
        else:
            out.append("No transcript.")
    out.append(f"---\n\n*Shared from Athena on "
               f"{local_time(settings, time.time(), '%Y-%m-%d')}.*")
    return "\n\n".join(out) + "\n"
