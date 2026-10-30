"""Reading the Messages database, read-only.

``~/Library/Messages/chat.db`` is opened with ``mode=ro``: never written, never copied.
SQLite's WAL lets this reader run while Messages writes. Columns that only newer macOS
has (``date_edited``, ``date_retracted``, ``thread_originator_guid``) are read when they
exist, so the same code reads macOS 10.15 and 13+.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from . import typedstream

APPLE_EPOCH = 978307200  # 2001-01-01T00:00:00Z
GROUP_STYLE = 43
# U+FFFC: where an attachment sits in a message's text
OBJECT_CHAR = "￼"
OPTIONAL = ("attributedBody", "associated_message_type", "item_type", "date_edited",
            "date_retracted", "thread_originator_guid")


def apple_time(value: Optional[int]) -> Optional[float]:
    """Unix seconds of an Apple date: nanoseconds since 2001 (macOS 10.13+) or seconds."""
    if not value:
        return None
    v = float(value)
    if abs(v) > 1e11:
        v /= 1e9
    return v + APPLE_EPOCH


@dataclasses.dataclass
class Chat:
    rowid: int
    guid: str
    name: Optional[str]
    group: bool
    handles: List[str]


@dataclasses.dataclass
class Message:
    rowid: int
    guid: str
    chat_rowid: Optional[int]
    at: Optional[float]
    from_me: bool
    handle: Optional[str]
    text: Optional[str]
    #: no text column, and attributedBody could not be decoded
    undecoded: bool
    edited_at: Optional[float]
    deleted: bool
    reply_to: Optional[str]
    #: a reaction (tapback) or a group event (rename, people added): never sent
    meta: bool
    attachments: List[Dict[str, Optional[str]]] = dataclasses.field(default_factory=list)
    #: the raw edit and unsend times (for the edit cursor)
    raw_edit: int = 0


class ChatDB:
    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        self.con = sqlite3.connect("file:" + quote(self.path) + "?mode=ro", uri=True,
                                   timeout=10)
        self.con.row_factory = sqlite3.Row
        self.columns = {r["name"] for r in self.con.execute("PRAGMA table_info(message)")}
        if not self.columns:
            raise sqlite3.DatabaseError("no message table: is this the Messages database?")

    def close(self) -> None:
        self.con.close()

    @property
    def has_edits(self) -> bool:
        return "date_edited" in self.columns or "date_retracted" in self.columns

    def _col(self, name: str) -> str:
        return ("m." + name) if name in self.columns else ("NULL AS " + name)

    # ------------------------------------------------------------------ reads
    def max_rowid(self) -> int:
        return self.con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]

    def chats(self) -> Dict[int, Chat]:
        handles: Dict[int, List[str]] = {}
        for r in self.con.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj"
                " JOIN handle h ON h.ROWID = chj.handle_id ORDER BY chj.chat_id, h.ROWID"):
            handles.setdefault(r[0], []).append(r[1])
        out = {}
        for r in self.con.execute("SELECT ROWID, guid, display_name, style FROM chat"):
            name = (r["display_name"] or "").strip() or None
            out[r["ROWID"]] = Chat(r["ROWID"], r["guid"], name, r["style"] == GROUP_STYLE,
                                   handles.get(r["ROWID"], []))
        return out

    def chat_stats(self) -> Dict[int, Tuple[int, Optional[float]]]:
        """chat ROWID -> (messages, the last message's time)."""
        return {r[0]: (r[1], apple_time(r[2])) for r in self.con.execute(
            "SELECT cmj.chat_id, COUNT(*), MAX(m.date) FROM chat_message_join cmj"
            " JOIN message m ON m.ROWID = cmj.message_id GROUP BY cmj.chat_id")}

    def _select(self, where: str, params: Iterable[object], limit: int) -> List[Message]:
        cols = ", ".join(self._col(c) for c in OPTIONAL)
        sql = ("SELECT m.ROWID AS rowid, m.guid, m.text, m.is_from_me, m.date, h.id AS handle,"
               " (SELECT MIN(chat_id) FROM chat_message_join WHERE message_id = m.ROWID)"
               " AS chat_rowid, " + cols +
               " FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id"
               " WHERE " + where + " ORDER BY m.ROWID LIMIT ?")
        rows = self.con.execute(sql, tuple(params) + (limit,)).fetchall()
        messages = [self._message(r) for r in rows]
        self._attach(messages)
        return messages

    def messages_after(self, rowid: int, limit: int) -> List[Message]:
        """Messages with a ROWID above ``rowid``, in ROWID order."""
        return self._select("m.ROWID > ?", (rowid,), limit)

    def chat_messages_after(self, chat_rowid: int, rowid: int, upto: int,
                            limit: int) -> List[Message]:
        """One chat's messages with ``rowid`` < ROWID <= ``upto`` (sending a chat again)."""
        return self._select(
            "m.ROWID > ? AND m.ROWID <= ? AND m.ROWID IN"
            " (SELECT message_id FROM chat_message_join WHERE chat_id = ?)",
            (rowid, upto, chat_rowid), limit)

    def changed_since(self, cursor: int, upto: int, limit: int) -> List[Message]:
        """Messages already sent (ROWID <= ``upto``) edited or unsent after ``cursor``
        (raw Apple time), oldest change first. Empty before macOS 13."""
        parts = [c + " > ?" for c in ("m.date_edited", "m.date_retracted")
                 if c[2:] in self.columns]
        if not parts:
            return []
        where = "m.ROWID <= ? AND (" + " OR ".join(parts) + ")"
        messages = self._select(where, (upto,) + (cursor,) * len(parts), 1_000_000)
        messages.sort(key=lambda m: (m.raw_edit, m.rowid))
        return messages[:limit]

    def edit_max(self) -> int:
        parts = [c for c in ("date_edited", "date_retracted") if c in self.columns]
        if not parts:
            return 0
        return max(self.con.execute(
            "SELECT COALESCE(MAX(" + p + "), 0) FROM message").fetchone()[0] for p in parts)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _message(r: sqlite3.Row) -> Message:
        text = r["text"]
        undecoded = False
        if not text and r["attributedBody"]:
            text = typedstream.decode(r["attributedBody"])
            undecoded = text is None
        if text:
            text = text.replace(OBJECT_CHAR, "").strip()
        meta = bool(r["associated_message_type"]) or bool(r["item_type"])
        edited = r["date_edited"] or 0
        retracted = r["date_retracted"] or 0
        from_me = bool(r["is_from_me"])
        return Message(
            rowid=r["rowid"], guid=r["guid"], chat_rowid=r["chat_rowid"],
            at=apple_time(r["date"]), from_me=from_me,
            handle=None if from_me else r["handle"], text=text or None,
            undecoded=undecoded, edited_at=apple_time(edited), deleted=bool(retracted),
            reply_to=r["thread_originator_guid"] or None, meta=meta,
            raw_edit=max(int(edited), int(retracted)))

    def _attach(self, messages: List[Message]) -> None:
        ids = [m.rowid for m in messages]
        if not ids:
            return
        by_id = {m.rowid: m for m in messages}
        tables: Set[str] = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"attachment", "message_attachment_join"} <= tables:
            return
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            for r in self.con.execute(
                    "SELECT maj.message_id, a.transfer_name, a.filename, a.mime_type"
                    " FROM message_attachment_join maj JOIN attachment a"
                    " ON a.ROWID = maj.attachment_id WHERE maj.message_id IN (" + marks + ")"
                    " ORDER BY maj.message_id, a.ROWID", part):
                name = r["transfer_name"] or os.path.basename(r["filename"] or "") or None
                by_id[r["message_id"]].attachments.append(
                    {"name": name, "type": r["mime_type"] or None})
