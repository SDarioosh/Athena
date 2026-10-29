"""Rows as JSON: one function per entity.

The MCP tools' ``format="json"`` rows (``mcp_server.py``; the hub's jobs read them, fields
listed in ``docs/api/mcp.md``) and the REST API's rows (``api.py``). The field names,
order and values are part of the contract with the hub (``tests/contract/snapshots``).
Each function takes the row and what was looked up for the whole list (written notes,
meetings, people, ...), so a list costs one query per kind, not one per row.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .dates import fact_valid_until
from .domain import short_title
from .links import local_time, transcript_link
from .timeutil import iso

# Subjects that mean the owner (the user) in a fact, besides the owner's own names.
OWNER_WORD_ORDER = ("me", "i", "myself", "owner", "the owner", "user", "the user")
OWNER_WORDS = set(OWNER_WORD_ORDER)


def owner_names(settings: Settings) -> tuple[str, ...]:
    """Names that mean the owner, in order: CONVO_OWNER_NAME, then the owner words."""
    names = [settings.owner_name.strip(), *OWNER_WORD_ORDER]
    return tuple(dict.fromkeys(n for n in names if n))


def said_at_link(settings: Settings, r: dict[str, Any]) -> str:
    """The link to where a row (``conversation_id``, ``source_at``, ``conversation_at``)
    was said."""
    return transcript_link(settings, r["conversation_id"],
                           (r["source_at"] or r["conversation_at"]) - r["conversation_at"])


# ------------------------------------------------------------- MCP rows
def action_item(r: dict[str, Any], settings: Settings, *, mine_names: set[str],
                participants: dict[str, list[dict[str, Any]]],
                written: dict[str, dict[str, Any]], summaries: dict[str, dict[str, Any]],
                meetings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A list_action_items JSON row."""
    owner = (r["owner"] or "").strip().lower()
    kind = r.get("kind") or "task"
    others = [] if kind == "note" else with_people(r["text"], participants.get(
        r["conversation_id"], []))
    return {"id": r["id"], "status": r["status"], "kind": r.get("kind") or "task",
            "mine": not owner or owner in mine_names,
            "title": r.get("title") or short_title(r.get("note_title"), r["text"]),
            "explicit": bool(r.get("explicit")),
            "text": r["text"], "owner": r["owner"], "due": r["due"],
            "due_date": r["due_date"], "due_time": r.get("due_time"),
            "duration_minutes": r.get("duration_minutes"), "location": r.get("location"),
            "list_name": r.get("list_name"), "note_title": r.get("note_title"),
            "time_unclear": bool(r.get("time_unclear")),
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "conversation_kind": r.get("conversation_kind"),
            "conversation_at": iso(r["conversation_at"]),
            "created_at": iso(r.get("created_at") or r["conversation_at"]),
            "timezone": settings.timezone, "link": said_at_link(settings, r), "with": others,
            "key_points": note_key_points(r, summaries.get(r["conversation_id"])),
            "source": source(r["conversation_id"], meetings),
            **written_json(written.get(r["conversation_id"]))}


def fact(f: dict[str, Any], settings: Settings, *, owners: set[str], people: set[str],
         owner_id: int | None, person_ids: dict[str, int | None],
         quotes: dict[int, str | None], written: dict[str, dict[str, Any]],
         meetings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A list_facts JSON row; ``person_ids`` maps a subject to its person (the
    subjects that are a person)."""
    who = about(f["subject"], owners, people)
    pid = (owner_id if who == "owner" else
           person_ids.get(f["subject"] or "") if who == "person" else None)
    return {"id": f["id"], "subject": f["subject"] or "", "about": who,
            "person_id": pid, "fact": f["fact"], "kind": f.get("kind"),
            "conversation_id": f["conversation_id"],
            "quote": quotes.get(f["id"]),
            "times_said": f["times_said"], "days": f["days"],
            "first_said": f["first_said"], "last_said": f["last_said"],
            "confirmed": (f["times_said"] >= settings.fact_confirm_sources
                          and f["days"] >= settings.fact_confirm_days),
            "valid_until": valid_until(f, settings.timezone),
            "link": said_at_link(settings, f),
            "source": source(f["conversation_id"], meetings),
            **written_json(written.get(f["conversation_id"]))}


def person(p: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """A list_people JSON row."""
    def day(ts: float | None) -> str | None:
        return (local_time(settings, ts, "%Y-%m-%d") or "?") if ts is not None else None

    return {"id": p["id"], "name": p["name"], "aliases": p["aliases"],
            "is_owner": p["is_owner"], "has_voice": p["has_voice"],
            "conversations": p["conversations"],
            "first_seen": day(p["first_at"]), "last_seen": day(p["last_at"])}


def people_order(speakers: list[dict[str, Any]], mentioned: list[dict[str, Any]],
                 people: list[dict[str, Any]],
                 person_id: Callable[[str], int | None]) -> list[dict[str, Any]]:
    """list_people(format="json")'s order, the text mode's: speakers by last heard, then
    people mentioned by last mention, then people never seen, by name."""
    rank: dict[int, int] = {}
    for name in [s["name"] for s in speakers] + [p["name"] for p in mentioned]:
        pid = person_id(name)
        if pid is not None:
            rank.setdefault(pid, len(rank))
    return sorted(people, key=lambda p: (rank.get(p["id"], len(rank)), p["name"].lower()))


# -------------------------------------------------------------- pieces
def valid_until(f: dict[str, Any], timezone: str) -> str | None:
    """The last day a fact about one occasion is worth keeping, or None."""
    said_on = datetime.fromtimestamp(f["conversation_at"], ZoneInfo(timezone)).date()
    end = fact_valid_until(f["fact"], said_on)
    return end.isoformat() if end else None


def about(subject: str | None, owners: set[str], people: set[str]) -> str:
    """Who a fact is about: "owner", "person" (a known speaker or a person named in a
    conversation) or "other"."""
    s = (subject or "").strip().lower()
    if s in owners:
        return "owner"
    return "person" if s and s in people else "other"


def with_people(text: str, people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """list_action_items JSON ``with``: the conversation's other people, those whose name
    (any name they appear under) is a word in the item's text first, then the rest in
    order of first appearance."""
    def named(p: dict[str, Any]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text or "", re.IGNORECASE)
                   for n in p["names"])

    ranked = sorted(people, key=lambda p: not named(p))
    return [{"id": p["id"], "name": p["name"]} for p in ranked]


def note_key_points(r: dict[str, Any], summary: dict[str, Any] | None) -> list[str]:
    """JSON rows' ``key_points`` (MCP 0.15.0): for a note that is a whole voice note (its
    text is the voice note's summary, see ``_lone_note`` in summarize/rules/notes.py), the
    voice note's key points, so the hub's note holds what was said ("Screen: 49 mm x
    60 mm"), not only the summary; ``[]`` for every other item."""
    if r.get("kind") != "note" or r.get("list_name") or not summary:
        return []
    said = " ".join((summary.get("summary") or "").split())
    if not said or said != " ".join((r.get("text") or "").split()):
        return []
    return [str(k).strip() for k in summary.get("key_points") or [] if str(k).strip()]


def source(conv_id: str, meetings: dict[str, dict[str, Any]]) -> str | None:
    """JSON rows' ``source`` (MCP 0.16.0): "meeting" when the fact or item comes from a
    meeting (Zoom today), else null (a recording, a voice note or a written note; those are
    told apart by ``conversation_kind`` and ``written_by_hand``)."""
    return "meeting" if conv_id in meetings else None


def written_json(written: dict[str, Any] | None) -> dict[str, Any]:
    """JSON rows' fields for a written note (MCP 0.12.0): ``written_by_hand`` and
    ``note_link``, the note in the hub (null for spoken ones)."""
    return {"written_by_hand": bool(written),
            "note_link": written["link"] if written else None}


# ------------------------------------------------------------- REST rows
def rest_conversation(c: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """A conversation as ``GET /v1/conversations[/{id}]`` returns it: the stored row with
    ISO times, links and its items' source links."""
    out = {k: v for k, v in c.items() if k not in ("started_at", "ended_at", "updated_at")}
    out["started_at"] = iso(c["started_at"])
    out["ended_at"] = iso(c["ended_at"])
    out["transcript_url"] = f"/v1/conversations/{c['id']}/transcript"
    out["link"] = transcript_link(settings, c["id"])
    for key in ("action_items", "facts"):
        for item in out.get(key, []):
            src = item.get("source_at")
            item["source_at"] = iso(src)
            if src is not None:
                item["source_url"] = transcript_link(settings, c["id"], src - c["started_at"])
    if out.get("meeting"):
        out["meeting"] = {**out["meeting"], "participants": [
            {**p, "joined_at": iso(p["joined_at"]), "left_at": iso(p["left_at"])}
            for p in out["meeting"]["participants"]]}
    for item in out.get("action_items", []):
        item["created_at"] = iso(item.get("created_at"))
        item["time_unclear"] = bool(item.get("time_unclear"))
        item["explicit"] = bool(item.get("explicit"))
    return out


def rest_action_item(r: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """An action item as ``GET /v1/action-items`` returns it: the stored row (changed in
    place) with ISO times, booleans and the timezone. Its shape is kept as it is."""
    r["source_at"], r["conversation_at"] = iso(r["source_at"]), iso(r["conversation_at"])
    r["created_at"] = iso(r.get("created_at"))
    r["time_unclear"] = bool(r.get("time_unclear"))
    r["explicit"] = bool(r.get("explicit"))
    r["timezone"] = settings.timezone
    return r
