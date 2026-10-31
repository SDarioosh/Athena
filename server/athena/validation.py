"""Input checks for what devices and the hub send: ids, tracks, written notes.

Each check raises ValueError with the message the REST API answers 400 with. Kept apart
from ``pipeline.py`` so the meeting modules can use them without importing the pipeline.
"""

from __future__ import annotations

import re

from . import audio as audio_mod
from .domain import CONVERSATION, SESSION_KINDS, WRITTEN_DEVICE, WRITTEN_SESSION_PREFIX

_SAFE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def validate_id(value: str, what: str) -> str:
    if not _SAFE.match(value):
        raise ValueError(f"{what} must match {_SAFE.pattern}")
    return value


# Tracks (POST /v1/ingest/chunk ``track``/``track_name``): one participant's audio in a
# session with several, e.g. a Zoom user id and display name.
TRACK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_TRACK_NAME_CHARS = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def validate_track(track: str | None, track_name: str | None) -> tuple[str, str | None]:
    """The stored (track, track_name): '' for no track (``track`` omitted or empty);
    the name with control characters dropped and spaces collapsed, None when empty.
    Raises ValueError for a bad track, a name over ``MAX_TRACK_NAME_CHARS`` characters,
    or a name without a track."""
    track = track or ""
    if track and not TRACK.match(track):
        raise ValueError(f"track must match {TRACK.pattern}")
    name = " ".join(_CONTROL_CHARS.sub("", track_name or "").split()) or None
    if name is not None and len(name) > MAX_TRACK_NAME_CHARS:
        raise ValueError(f"track_name is longer than {MAX_TRACK_NAME_CHARS} characters")
    if name is not None and not track:
        raise ValueError("track_name needs a track")
    return track, name


# Written notes (POST /v1/ingest/text): the hub's stable id of a note, and size limits
# (a hub note has at most 60 pages; a dense handwritten page is ~2,000 characters).
SOURCE_ID = re.compile(r"^hub-note:[A-Za-z0-9._-]{1,100}$")
MAX_TEXT_CHARS = 200_000
MAX_TEXT_LINES = 5_000
MAX_LINE_CHARS = 4_000
MAX_TITLE_CHARS = 200
MAX_LINK_CHARS = 2_000
# control characters other than tab and newlines
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# written_at must be a plausible time: 2000-01-01 .. 2100-01-01 UTC
EARLIEST, LATEST = 946_684_800.0, 4_102_444_800.0


def written_lines(text: str) -> list[str]:
    """A written note's text as lines: split at line breaks, control characters
    dropped, surrounding spaces trimmed, empty lines left out, a line longer than
    ``MAX_LINE_CHARS`` wrapped at the last space before it (cut when there is none).
    Raises ValueError when nothing is left or there are too many lines."""
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text is longer than {MAX_TEXT_CHARS} characters")
    lines = [part for ln in (" ".join(_CONTROL.sub("", raw).split())
                             for raw in text.splitlines()) if ln for part in _wrap(ln)]
    if not lines:
        raise ValueError("text has no words; to remove a note use DELETE "
                         "/v1/ingest/text/{source_id}")
    if len(lines) > MAX_TEXT_LINES:
        raise ValueError(f"text has more than {MAX_TEXT_LINES} lines")
    return lines


def _wrap(line: str) -> list[str]:
    """``line`` (single spaces, trimmed) in parts of at most ``MAX_LINE_CHARS``."""
    out = []
    while len(line) > MAX_LINE_CHARS:
        cut = line.rfind(" ", 0, MAX_LINE_CHARS + 1)
        if cut <= 0:
            cut = MAX_LINE_CHARS
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    return out + [line] if line else out


def validate_source_id(source_id: str) -> str:
    if not SOURCE_ID.match(source_id or ""):
        raise ValueError("source_id must look like hub-note:<id> "
                         "(id: letters, digits, '.', '_' or '-', at most 100)")
    return source_id


# characters a link may not have: they would end "(written by hand: <link>)" in the MCP
# text or quote it; brackets only around an IPv6 host
_LINK_BAD = re.compile(r"[\s\x00-\x1f\x7f()<>\"'`{}|\\^]")
_LINK = re.compile(r"^https?://(\[[0-9A-Fa-f:.]+\]|[^\s/?#\[\]@]+)(:\d+)?([/?#][^\[\]]*)?$")


def validate_link(link: str) -> str:
    """The hub URL of the note: http(s), no spaces, control characters, quotes,
    parentheses or angle brackets (it is shown in parentheses), square brackets only
    around an IPv6 host."""
    link = (link or "").strip()
    if len(link) > MAX_LINK_CHARS or not _LINK.match(link) or _LINK_BAD.search(link):
        raise ValueError(f"link must be an http(s) URL of at most {MAX_LINK_CHARS} "
                         "characters, without spaces, quotes, ( ) < > [ ] { } | \\ ^ `")
    return link


def check_chunk(*, device: str, session_id: str, sequence: int, kind: str | None,
                codec: str, data: bytes, track: str | None,
                track_name: str | None) -> tuple[str, str, str, str | None]:
    """The checks on one uploaded chunk, in the order they are made (the first wrong
    field is the one named). Returns the stored (kind, codec, track, track_name)."""
    validate_id(device, "device")
    validate_id(session_id, "session_id")
    track, track_name = validate_track(track, track_name)
    if device == WRITTEN_DEVICE or session_id.startswith(WRITTEN_SESSION_PREFIX):
        raise ValueError(f"device {WRITTEN_DEVICE!r} and session ids starting with "
                         f"{WRITTEN_SESSION_PREFIX!r} are for written notes "
                         "(POST /v1/ingest/text)")
    if sequence < 0:
        raise ValueError("sequence must be >= 0")
    kind = (kind or CONVERSATION).strip().lower()
    if kind not in SESSION_KINDS:
        raise ValueError(f"kind must be one of {list(SESSION_KINDS)}")
    codec = codec.lower()
    if codec not in audio_mod.KNOWN_CODECS:
        raise ValueError(f"codec must be one of {sorted(audio_mod.KNOWN_CODECS)}")
    if not data:
        raise ValueError("empty audio")
    return kind, codec, track, track_name


def check_written_note(*, source_id: str, link: str, written_at: float,
                       title: str | None) -> tuple[str, str | None]:
    """The checks on a written note before its text (``written_lines``), in the order
    they are made. Returns the stored (link, title): the link trimmed, the title with
    control characters dropped and spaces collapsed, None when empty."""
    validate_source_id(source_id)
    link = validate_link(link)
    if not (EARLIEST <= written_at < LATEST):
        raise ValueError("written_at must be a date between 2000 and 2100")
    title = " ".join(_CONTROL.sub("", title or "").split()) or None
    if title is not None and len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"title is longer than {MAX_TITLE_CHARS} characters")
    return link, title
