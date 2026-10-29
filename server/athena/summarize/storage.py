"""The model's summary as storage-ready dicts (action items with their list, facts)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..store import NOTE_ITEM, clean_list_name, person_name, short_title
from .rules.facts import fact_kind
from .rules.notes import _note_storage
from .rules.text import _NAME_WORD
from .schemas import ActionItem, ConversationSummary

_PLACEHOLDER = re.compile(r"^(speaker\s*\d+|unknown|user|the user)$", re.IGNORECASE)


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_date(value: str | None) -> str | None:
    """A valid YYYY-MM-DD or None (models sometimes return 'Friday' or '2026-02-30')."""
    if not value or not _DATE.match(value.strip()):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def to_storage(s: ConversationSummary, origin: float) -> dict[str, Any]:
    def at(x: int | None) -> float | None:
        return origin + x if x is not None and x >= 0 else None

    return {
        "title": s.title.strip(),
        "summary": s.summary.strip(),
        "key_points": [k.strip() for k in s.key_points if k.strip()],
        "topics": _dedupe(s.topics),
        "people": [p for p in _dedupe([person_name(p) for p in s.people])
                   if not _PLACEHOLDER.match(p)],
        "action_items": [_item_storage(a, at(a.at_seconds)) for a in s.action_items],
        "facts": [{"subject": f.subject, "fact": f.fact, "source_at": at(f.at_seconds),
                   "kind": fact_kind(f.kind)} for f in s.facts],
    }


_REPEATING = re.compile(r"\b(every|each)\s+(day|week|month|year|morning|evening|night|"
                        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b|"
                        r"\b(daily|weekly|monthly|fortnightly)\b", re.IGNORECASE)
_REPEAT_PREFIX = "Set up a repeating event: "


def _item_storage(a: ActionItem, source_at: float | None) -> dict[str, Any]:
    """One action item for storage; ``time`` (as said) is resolved later and dropped."""
    kind, text = a.kind, a.text.strip()
    # a repeating event can't be filed as one calendar event: a task to set it up
    if kind == "event" and _REPEATING.search(f"{text} {a.due or ''} {a.time or ''}"):
        kind = "task"
        if not text.lower().startswith(_REPEAT_PREFIX.lower()):
            text = _REPEAT_PREFIX + text[:1].lower() + text[1:]
    duration = a.duration_minutes if a.duration_minutes and 0 < a.duration_minutes <= 1440 \
        else None
    if kind == NOTE_ITEM:
        return _note_storage(a, text, source_at)
    text, list_ = _list_and_text(text, a.list_name)
    return {"text": text, "title": short_title(a.title, text), "kind": kind,
            "owner": a.owner, "due": a.due, "due_date": _iso_date(a.due_date),
            "time": a.time, "duration_minutes": duration if kind == "event" else None,
            "location": (a.location or "").strip() or None, "list_name": list_,
            "note_title": None, "source_at": source_at}


# "... to my shopping list", "on the work to-do list", "to shopping list": a list the
# user named. "my" / "the" is optional (speech and models drop it), so a name word is
# never one ("add a reminder to check the list" names no list).
_LIST_PHRASE = re.compile(
    r"\s*\b(?:to|on|onto|in|into|for)\s+(?:(?:my|the|our)\s+)?"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+(?:to-?do\s+|to\s+do\s+)?list\b",
    re.IGNORECASE)
# "please add ...", "can you put ...": the polite lead-in goes with the command word
_ADD = re.compile(r"^(?:(?:please|(?:can|could|would|will)\s+you)(?:\s+please)?[\s,]+)?"
                  r"(?:add|put|write(?:\s+down)?)\s+", re.IGNORECASE)


def _list_and_text(text: str, said: str | None) -> tuple[str, str | None]:
    """The item's text and list. The list is the model's ``list_name``, else an "add X
    to my shopping list" phrase in the text; a phrase naming that list is taken out of
    the text ("Add milk to my shopping list" -> "Milk")."""
    name = clean_list_name(said)
    m = _LIST_PHRASE.search(text)
    said_in_text = clean_list_name(m["name"]) if m else None
    if said_in_text is None:
        return text, name
    if name is None and _ADD.match(text):
        name = said_in_text
    if name is None or name.lower() != said_in_text.lower():
        return text, name
    rest = _ADD.sub("", (text[:m.start()] + text[m.end():]).strip(" ,.")).strip()
    return (rest[:1].upper() + rest[1:] if rest else text), name


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for i in items:
        k = i.strip()
        if k and k.lower() not in seen:
            seen[k.lower()] = k
    return list(seen.values())
