"""Action items: listing and updating them."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..domain import (
    ITEM_KINDS,
    ITEM_STATUSES,
    NOTE_ITEM,
    UNSET,
    clean_list_name,
    is_explicit,
    short_title,
)


def _valid_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalize_hhmm(value: str) -> str | None:
    """'9:05' / '09:05' / '17:30' -> zero-padded 'HH:MM' (24 h), else None."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


class ActionItemsRepo:
    """``Store``'s methods for action items. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    def list_action_items(self, status: str | None = "open", *,
                          notes: bool = True) -> list[dict[str, Any]]:
        """Newest conversation first. ``notes=False`` leaves out note items (they are no
        to-dos)."""
        sql = ("SELECT a.*, c.title AS conversation_title, c.started_at AS conversation_at,"
               " c.kind AS conversation_kind"
               " FROM action_items a JOIN conversations c ON c.id = a.conversation_id"
               " WHERE 1=1")
        params: list[Any] = []
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        if not notes:
            sql += " AND a.kind IS NOT ?"
            params.append(NOTE_ITEM)
        return self._q(sql + " ORDER BY c.started_at DESC, a.id", tuple(params))

    def get_action_item(self, item_id: int) -> dict[str, Any] | None:
        return self._one(
            "SELECT a.*, c.title AS conversation_title, c.started_at AS conversation_at,"
            " c.kind AS conversation_kind FROM action_items a"
            " JOIN conversations c ON c.id = a.conversation_id WHERE a.id = ?", (item_id,))

    def set_action_item_status(self, item_id: int, status: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE action_items SET status = ? WHERE id = ?",
                             (status, item_id)).rowcount > 0

    def update_action_item(self, item_id: int, *, status: str | None = None,
                           kind: str | None = None, due_date: str | None = UNSET,
                           due_time: str | None = UNSET,
                           list_name: str | None = UNSET,
                           title: str | None = UNSET) -> dict[str, Any] | None:
        """Change an item's status, kind, due date, time of day, to-do list or title
        (``None`` clears a date, time or list and gives the title back from the text,
        ``UNSET`` leaves it). Raises ``ValueError`` for bad values; returns the updated
        item, or None if there is none with that id. A changed kind, date, time, list or
        title survives re-summaries; setting the time also clears ``time_unclear``. An
        item the user gives a date, time, list or reminder/event kind becomes explicit."""
        sets: dict[str, Any] = {}
        if status is not None:
            if status not in ITEM_STATUSES:
                raise ValueError("status must be open, done or dismissed")
            sets["status"] = status
        if kind is not None:
            if kind not in ITEM_KINDS:
                raise ValueError("kind must be task, event or reminder")
            sets["kind"] = kind
        if due_date is not UNSET:
            if due_date is not None and not _valid_date(due_date):
                raise ValueError("due_date must be YYYY-MM-DD (or 'none' to clear it)")
            sets["due_date"] = due_date
        if due_time is not UNSET:
            if due_time is not None:
                due_time = normalize_hhmm(due_time)
                if due_time is None:
                    raise ValueError("due_time must be HH:MM, 24 h (or 'none' to clear it)")
            sets["due_time"] = due_time
            sets["time_unclear"] = 0
        if list_name is not UNSET:
            if list_name is not None:
                list_name = clean_list_name(list_name)
                if list_name is None:
                    raise ValueError("list_name must be a short list name like 'shopping' "
                                     "(or 'none' to clear it)")
            sets["list_name"] = list_name
        if title is not UNSET:
            sets["title"] = title  # resolved below: the text is needed for a cleared one
        if not sets:
            raise ValueError("nothing to change: give status, kind, due_date, due_time, "
                             "list_name or title")
        if sets.keys() - {"status"}:
            sets["edited"] = 1
        with self.tx() as c:
            if "title" in sets:
                r = c.execute("SELECT text FROM action_items WHERE id = ?",
                              (item_id,)).fetchone()
                sets["title"] = short_title(title, r["text"] if r else "")
                # a renamed note (or one whose title went back to its text): the hub files
                # it by note_title
                sets["note_title"] = sets["title"]
            cols = [f"{k} = ?" if k != "note_title"
                    else "note_title = CASE WHEN kind = 'note' THEN ? ELSE note_title END"
                    for k in sets]
            n = c.execute(f"UPDATE action_items SET {', '.join(cols)}"
                          " WHERE id = ?", (*sets.values(), item_id)).rowcount
            if n:
                r = c.execute("SELECT kind, due_date, due_time, list_name FROM action_items"
                              " WHERE id = ?", (item_id,)).fetchone()
                if is_explicit(dict(r)):
                    c.execute("UPDATE action_items SET explicit = 1 WHERE id = ?", (item_id,))
        return self.get_action_item(item_id) if n else None
