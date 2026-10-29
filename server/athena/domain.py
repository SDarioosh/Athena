"""What Athena's data is made of, apart from the database: session, line and
conversation kinds, statuses, a transcript line (``Segment``), and the rules for
action item titles, list names and person names. Nothing here imports the store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MEETING_ACTIVE = ("starting", "waiting_room", "waiting_permission", "recording")
_ACTIVE_IN = ", ".join("?" * len(MEETING_ACTIVE))

_PLACEHOLDER_SPEAKER = re.compile(r"^(speaker\s*\d+|unknown)$", re.IGNORECASE)

# What an erased transcript line says instead (erase_fact)
ERASED_TEXT = "[forgotten]"


# Session kinds (sessions.kind). A voice note's conversation also gets conversations.kind
# NOTE; ordinary conversations get the kind the summary triage picks.
CONVERSATION = "conversation"
NOTE = "note"
SESSION_KINDS = (CONVERSATION, NOTE)
# The device of written notes (POST /v1/ingest/text); no recorder uploads as it.
WRITTEN_DEVICE = "handwriting"
# list_conversations' ``kind`` beyond the session kinds: the two sorts of NOTE
# (MCP 0.14.0). NOTE itself is both.
VOICE = "voice"      # notes recorded out loud
WRITTEN = "written"  # notes written by hand in the hub (WRITTEN_DEVICE)
# ... and (MCP 0.18.0) CONVERSATION split in two: online meetings (a meeting_info row)
# and the other conversations, recorded in person.
MEETING = "meeting"
IN_PERSON = "in_person"
# A written note's session id is this prefix plus its source id.
WRITTEN_SESSION_PREFIX = "written:"


# Transcript line kinds (segments.kind): spoken, or typed in a meeting's chat.
SPEECH = "speech"
CHAT = "chat"
# The device the Zoom bot uploads as.
ZOOM_DEVICE = "zoom"

# A meeting in one of these statuses takes no new chunk (``MeetingClosed``).
MEETING_CLOSED_STATUSES = ("processing", "done")


@dataclass
class Segment:
    started_at: float
    ended_at: float
    text: str
    speaker: str = "Unknown"
    is_owner: bool = False
    id: int | None = None
    session_id: str | None = None
    device: str | None = None
    conversation_id: str | None = None
    kind: str = SPEECH  # CHAT: a message typed in a meeting's chat, not transcribed


ITEM_STATUSES = ("open", "done", "dismissed")
ITEM_KINDS = ("task", "event", "reminder")
# an action item that is a note the user asked a voice note to save; the hub files it
# into its notes (update_action_item can change it to another kind, not make one)
NOTE_ITEM = "note"
UNSET: Any = object()  # "not given" for optional updates (None means "clear")
# an action item's title (the hub's to-do or note title) is at most this long
TITLE_MAX = 60
# kinds that are the user's own items, filed without asking: a reminder or event someone
# asked for, a note the user asked a voice note to save
EXPLICIT_KINDS = ("reminder", "event", NOTE_ITEM)


def short_title(title: str | None, text: str | None) -> str:
    """An action item's title: ``title`` (the model's or the user's), else the start of
    its text; at most ``TITLE_MAX`` characters, cut at a word boundary, without a
    sentence's closing period."""
    t = re.sub(r"\s+", " ", (title or "").strip().strip("\"'")).strip()
    if not t:
        t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) > TITLE_MAX:
        head = t[:TITLE_MAX + 1]
        t = head.rsplit(" ", 1)[0] if " " in head else t[:TITLE_MAX]
    return t.rstrip(" .,;:-") or t


# "Marisol's" / "James'": a possessive the summary model left on a name
_POSSESSIVE = re.compile(r"(?<=\w)['’]s$|(?<=s)['’]$", re.IGNORECASE)


def person_name(name: str) -> str:
    """A person's name without a possessive ending: "Ana's" -> "Ana", "James'" -> "James"."""
    return _POSSESSIVE.sub("", name.strip())


def is_explicit(item: dict[str, Any]) -> bool:
    """The item says when or where it goes: a date, a time, a named list, or a kind the
    user asks for (reminder, event, note). The summarizer also counts command words."""
    return bool(item.get("due_date") or item.get("due_time") or item.get("list_name")
                or item.get("kind") in EXPLICIT_KINDS)


def _device_name(name: str) -> bool:
    """A Zoom display name with no person in it: a device, a room or conference hardware
    ("iPhone", "Zoom user", "Conference Room 2"). Only meetings get here."""
    from .meeting_names import clean_display_name, is_room_or_hardware

    return is_room_or_hardware(name) or not clean_display_name(name)


_LIST_NOISE = re.compile(r"^(?:my|the|our|a)\s+|\s+(?:to-?do\s+|to\s+do\s+)?list$",
                         re.IGNORECASE)


# the general to-do list ("put it on my to-do list") is no named list
_NO_LIST = {"to-do", "todo", "to do", "to-dos", "todos", "list"}


def clean_list_name(value: str | None) -> str | None:
    """A list name as the user said it, without "my", "the" and a trailing "list"
    ("my Shopping list" -> "Shopping"); None when nothing is left or it's too long."""
    name = re.sub(r"\s+", " ", (value or "").strip().strip(".\"'"))
    for _ in range(2):
        name = _LIST_NOISE.sub("", name).strip()
    if not name or len(name) > 40 or name.lower() in _NO_LIST:
        return None
    return name


_NOTES_NOISE = re.compile(r"^(?:my|the|our|a)\s+|\s+notes?$", re.IGNORECASE)


def clean_notes_name(value: str | None) -> str | None:
    """The notes a note goes into, as the user said them, without "my", "the" and a
    trailing "notes" ("my Hardware notes" -> "Hardware"); None for plain "my notes",
    nothing usable, or too long."""
    name = re.sub(r"\s+", " ", (value or "").strip().strip(".\"'"))
    for _ in range(2):
        name = _NOTES_NOISE.sub("", name).strip()
    if not name or len(name) > 40 or name.lower() in {"note", "notes", "none", "null"}:
        return None
    return name
