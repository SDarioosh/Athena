"""``TextsRepo``: the ``Store`` methods for text messages (docs/messages.md).

An upload is stored in one transaction: receipts and lines first, then every chat it
changed is threaded again over the stretch the change touches (``_rethread``). Threads
are conversations, so everything that reads conversations (search, transcripts, facts,
the hub's jobs) reads text threads too.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ..db.carry_over import _carry_over, _knowledge
from ..db.conversations import _delete_conversation, _move_knowledge, _says_words
from ..db.forget import _take_down_digests
from ..db.matching import _line_words
from ..db.people import _add_alias, _ensure_person, _live_person
from ..domain import CONVERSATION, ERASED_TEXT, TEXT_LINE, TEXT_SESSION_PREFIX
from . import threads
from .contract import Message, Upload


def session_id(chat_id: int) -> str:
    return f"{TEXT_SESSION_PREFIX}{chat_id}"


def label(text: dict[str, Any] | None) -> str:
    """How a thread is named next to its title: "Text messages with Mia Chen", "Group
    text: Weekend crew", "Group text with Mia Chen, Leo Park"; "" for no thread."""
    if not text:
        return ""
    people = ", ".join(text["people"]) or "unknown"
    if text["group"]:
        return f"Group text: {text['name']}" if text["name"] else f"Group text with {people}"
    return f"Text messages with {people}"


def plain_title(text: dict[str, Any]) -> str:
    """A thread's title while it has no summary: "Texts with Mia Chen"."""
    people = ", ".join(text["people"]) or "unknown"
    if text["group"]:
        return text["name"] or f"Group texts with {people}"
    return f"Texts with {people}"


def _handle_name(c: sqlite3.Connection, r: sqlite3.Row) -> str:
    """A handle's name from a row with its ``handle``, contact ``name``, ``person_id``,
    the person's name (``person``) and ``merged_into``: the person's current name, else
    the contact name, else the handle."""
    if r["person_id"] is not None:
        if r["merged_into"] is None and r["person"]:
            return r["person"]
        live = _live_person(c, r["person_id"])
        if live is not None:
            return live["name"]
    return r["name"] or r["handle"]


class _Upload:
    """One upload's work inside its transaction: caches what it looked up."""

    def __init__(self, c: sqlite3.Connection, up: Upload, owner: str, now: float) -> None:
        self.c, self.up, self.owner, self.now = c, up, owner, now
        self.chats: dict[str, int] = {}
        self._speakers: dict[str, str] = {}
        self._erased: list[set[str]] | None = None
        # contact renames to apply once the upload is stored (rename_speaker_everywhere)
        self.renames: list[tuple[str, str]] = []
        self.counts = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "skipped": 0}
        # chat row id -> the times of its new, changed or removed lines
        self.changed: dict[int, list[float]] = defaultdict(list)
        self.relabel: set[int] = set()

    # ---------------------------------------------------------------- handles
    def handle(self, handle: str, name: str | None, named: bool) -> None:
        """Store a handle; ``named``: the upload says what the contacts call it (``name``,
        None = no contact), else only make sure the handle is known."""
        c, src = self.c, self.up.source
        row = c.execute("SELECT name, person_id FROM text_handles WHERE source = ? AND"
                        " handle = ?", (src, handle)).fetchone()
        if row is not None and not named:
            return
        pid = row["person_id"] if row is not None else None
        old = row["name"] if row is not None else None
        if named and name and name != old:
            live = _live_person(c, pid) if pid is not None else None
            if live is None:
                pid = _ensure_person(c, name, created_at=self.now)
            elif old and live["name"].lower() == old.lower():
                # the contact was renamed and Athena still uses its old name: follow it
                self.renames.append((live["name"], name))
            else:  # the owner named this person in Athena: that name wins
                _add_alias(c, live["id"], name)
        c.execute("INSERT INTO text_handles (source, handle, name, person_id, updated_at)"
                  " VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, handle) DO UPDATE SET"
                  " name = excluded.name, person_id = excluded.person_id,"
                  " updated_at = excluded.updated_at",
                  (src, handle, name if named else old, pid, self.now))
        self._speakers.pop(handle, None)
        for (chat_id,) in c.execute("SELECT chat_id FROM text_chat_handles WHERE handle = ?"
                                    " AND chat_id IN (SELECT id FROM text_chats WHERE"
                                    " source = ?)", (handle, src)):
            self.relabel.add(chat_id)

    def speaker(self, handle: str) -> str:
        """The name a handle's lines show: its person's name, else its contact name, else
        the handle itself."""
        if handle not in self._speakers:
            row = self.c.execute("SELECT name, person_id FROM text_handles WHERE source = ?"
                                 " AND handle = ?", (self.up.source, handle)).fetchone()
            live = _live_person(self.c, row["person_id"]) \
                if row is not None and row["person_id"] is not None else None
            self._speakers[handle] = (live["name"] if live is not None else
                                      row["name"] if row is not None and row["name"]
                                      else handle)
        return self._speakers[handle]

    # ------------------------------------------------------------------ chats
    def chat(self, key: str, name: str | None, group: bool, handles: Iterable[str]) -> None:
        c, src = self.c, self.up.source
        row = c.execute("SELECT id, name, is_group FROM text_chats WHERE source = ? AND"
                        " chat_key = ?", (src, key)).fetchone()
        if row is None:
            chat_id = c.execute(
                "INSERT INTO text_chats (source, chat_key, name, is_group, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (src, key, name, int(group), self.now, self.now)).lastrowid
            assert chat_id is not None
            c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                      " received_at, status, stop_reason, kind)"
                      " VALUES (?, ?, ?, ?, ?, 'done', 'text', ?)",
                      (session_id(chat_id), src, self.now, self.now, self.now, CONVERSATION))
        else:
            chat_id = row["id"]
            if (row["name"], bool(row["is_group"])) != (name, group):
                c.execute("UPDATE text_chats SET name = ?, is_group = ?, updated_at = ?"
                          " WHERE id = ?", (name, int(group), self.now, chat_id))
                self.relabel.add(chat_id)
        before = {r[0] for r in c.execute("SELECT handle FROM text_chat_handles WHERE"
                                          " chat_id = ?", (chat_id,))}
        after = set(handles)
        if before != after:
            c.execute("DELETE FROM text_chat_handles WHERE chat_id = ?", (chat_id,))
            c.executemany("INSERT INTO text_chat_handles VALUES (?, ?)",
                          [(chat_id, h) for h in sorted(after)])
            self.relabel.add(chat_id)
        for h in after:
            self.handle(h, None, named=False)
        self.chats[key] = chat_id

    def chat_id(self, key: str, where: str) -> int:
        if key not in self.chats:
            row = self.c.execute("SELECT id FROM text_chats WHERE source = ? AND chat_key = ?",
                                 (self.up.source, key)).fetchone()
            if row is None:
                raise ValueError(f"{where}.chat {key!r} is unknown: send it in chats")
            self.chats[key] = row["id"]
        return self.chats[key]

    # --------------------------------------------------------------- messages
    def says_erased(self, text: str) -> bool:
        """The line says a fact the owner forgot (erase_fact): it is stored forgotten."""
        if self._erased is None:
            self._erased = [w for w in (_line_words(r[0]) for r in self.c.execute(
                "SELECT fact FROM facts WHERE erased_at IS NOT NULL"
                " UNION SELECT fact FROM erased_facts")) if w]
        words = _line_words(text)
        return any(_says_words(words, f) for f in self._erased)

    def message(self, m: Message, where: str) -> None:
        c, src = self.c, self.up.source
        chat_id = self.chat_id(m.chat, where)
        if m.handle is not None:
            self.handle(m.handle, None, named=False)
        row = c.execute("SELECT id, segment_id, text_hash, edited_at, deleted FROM"
                        " text_messages WHERE source = ? AND guid = ?",
                        (src, m.guid)).fetchone()
        atts = json.dumps([{"name": a.name, "type": a.type} for a in m.attachments],
                          ensure_ascii=False)
        if row is None:
            line = m.line
            segment_id = None
            if line and not m.deleted:
                erased = self.says_erased(line)
                speaker = self.owner if m.from_me else self.speaker(m.handle or "")
                segment_id = c.execute(
                    "INSERT INTO segments (session_id, conversation_id, device, started_at,"
                    " ended_at, speaker, is_owner, text, erased, kind)"
                    " VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id(chat_id), src, m.at, m.at, speaker, int(m.from_me),
                     ERASED_TEXT if erased else line, int(erased), TEXT_LINE)).lastrowid
                self.changed[chat_id].append(m.at)
            c.execute("INSERT INTO text_messages (source, guid, chat_id, segment_id, sent_at,"
                      " from_me, handle, text_hash, edited_at, deleted, reply_to, attachments)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (src, m.guid, chat_id, segment_id, m.at, int(m.from_me), m.handle,
                       m.text_hash, m.edited_at, int(m.deleted), m.reply_to, atts))
            self.counts["added" if segment_id else "deleted" if m.deleted else "skipped"] += 1
            return
        if row["deleted"] or (row["text_hash"] == m.text_hash
                              and row["edited_at"] == m.edited_at and not m.deleted):
            self.counts["unchanged"] += 1  # unsent stays unsent; a resend changes nothing
            return
        seg = c.execute("SELECT started_at, erased FROM segments WHERE id = ?",
                        (row["segment_id"],)).fetchone() if row["segment_id"] else None
        if m.deleted or not m.line:
            if seg is not None:
                c.execute("DELETE FROM segments WHERE id = ?", (row["segment_id"],))
                self.changed[chat_id].append(seg["started_at"])
            c.execute("UPDATE text_messages SET deleted = ?, segment_id = NULL, text_hash = ?,"
                      " edited_at = ?, attachments = ? WHERE id = ?",
                      (int(m.deleted), m.text_hash, m.edited_at, atts, row["id"]))
            self.counts["deleted"] += 1
            return
        c.execute("UPDATE text_messages SET text_hash = ?, edited_at = ?, attachments = ?"
                  " WHERE id = ?", (m.text_hash, m.edited_at, atts, row["id"]))
        if seg is not None and not seg["erased"]:  # a forgotten line stays forgotten
            c.execute("UPDATE segments SET text = ? WHERE id = ?", (m.line, row["segment_id"]))
            self.changed[chat_id].append(seg["started_at"])
            self.counts["updated"] += 1
        else:  # the line was pruned or forgotten: an edit can't bring it back
            self.counts["unchanged"] += 1


class TextsRepo:
    """``Store``'s methods for text messages. A mixin: ``Store`` opens the connection they
    use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``) and has ``_index_transcript``,
    ``rename_everywhere`` and ``compact``."""

    # ------------------------------------------------------------------ upload
    def put_text_upload(self, up: Upload, *, owner: str, gap: float,
                        now: float | None = None) -> dict[str, Any]:
        """Store an upload (see ``contract``) and thread its chats again. Raises ValueError
        for a message of an unknown chat; then nothing is stored."""
        now = time.time() if now is None else now
        threads_out = {"changed": 0, "created": 0, "removed": 0}
        with self.tx() as c:  # type: ignore[attr-defined]
            u = _Upload(c, up, owner, now)
            for h in up.handles:
                u.handle(h.handle, h.name, named=True)
            for ch in up.chats:
                u.chat(ch.key, ch.name, ch.group, ch.handles)
            for i, m in enumerate(up.messages):
                u.message(m, f"messages[{i}]")
            for h in up.handles:  # lines of a handle whose name is new (no person yet)
                self._rename_handle_lines(c, up.source, h.handle, u.speaker(h.handle))
            for chat_id, times in u.changed.items():
                for k, v in self._rethread(c, chat_id, times, gap, now).items():
                    threads_out[k] += v
                c.execute("UPDATE sessions SET last_chunk_at = MAX(last_chunk_at, ?),"
                          " received_at = ? WHERE id = ?",
                          (max(times), now, session_id(chat_id)))
            for chat_id in u.relabel - set(u.changed):
                for (conv_id,) in c.execute("SELECT conversation_id FROM text_threads WHERE"
                                            " chat_id = ?", (chat_id,)).fetchall():
                    self._reindex(c, conv_id)
        for old, new in u.renames:  # a contact renamed: everywhere, like the owner would
            self.rename_everywhere(old, new)  # type: ignore[attr-defined]
        return {"messages": u.counts, "threads": threads_out}

    def _rename_handle_lines(self, c: sqlite3.Connection, source: str, handle: str,
                             name: str) -> None:
        """Lines of ``handle`` that still show the bare handle take its name."""
        if name == handle:
            return
        where = (" WHERE speaker = ? AND id IN (SELECT segment_id FROM text_messages"
                 " WHERE source = ? AND handle = ?)")
        convs = {r[0] for r in c.execute("SELECT DISTINCT conversation_id FROM segments"
                                         + where, (handle, source, handle)) if r[0]}
        c.execute("UPDATE segments SET speaker = ?" + where, (name, handle, source, handle))
        for conv_id in convs:
            self._mark_changed(c, conv_id, time.time())
            self._reindex(c, conv_id)

    # --------------------------------------------------------------- threading
    def _rethread(self, c: sqlite3.Connection, chat_id: int, times: list[float],
                  gap: float, now: float) -> dict[str, int]:
        """Thread the stretch of a chat around ``times`` again (docs/messages.md,
        "Threads"): from the start of the first thread it touches to the end of the last."""
        lo, hi = min(times), max(times)
        olds = c.execute(
            "SELECT v.id, v.started_at, v.ended_at FROM text_threads t JOIN conversations v"
            " ON v.id = t.conversation_id WHERE t.chat_id = ? AND v.ended_at >= ?"
            " AND v.started_at <= ?", (chat_id, lo - gap, hi + gap)).fetchall()
        start = min([lo] + [o["started_at"] for o in olds])
        end = max([hi] + [o["ended_at"] for o in olds])
        lines = [threads.Line(r[0], r[1], r[2]) for r in c.execute(
            "SELECT s.id, s.started_at, s.conversation_id FROM text_messages m"
            " JOIN segments s ON s.id = m.segment_id WHERE m.chat_id = ?"
            " AND s.started_at >= ? AND s.started_at <= ? ORDER BY s.started_at, s.id",
            (chat_id, start, end))]
        groups = threads.split(lines, gap)
        plan = threads.plan(groups)
        out = {"changed": 0, "created": 0, "removed": 0}
        source = c.execute("SELECT source FROM text_chats WHERE id = ?",
                           (chat_id,)).fetchone()[0]
        kept: list[str] = []
        for group, conv_id in zip(groups, plan.ids, strict=True):
            created = conv_id is None
            if conv_id is None:
                conv_id = uuid.uuid4().hex
                c.execute("INSERT INTO conversations (id, device, started_at, ended_at,"
                          " updated_at) VALUES (?, ?, ?, ?, ?)",
                          (conv_id, source, group[0].at, group[-1].at, now))
                c.execute("INSERT INTO text_threads (conversation_id, chat_id) VALUES (?, ?)",
                          (conv_id, chat_id))
                out["created"] += 1
            moved = [(conv_id, x.segment_id) for x in group if x.thread != conv_id]
            c.executemany("UPDATE segments SET conversation_id = ? WHERE id = ?", moved)
            kept.append(conv_id)
            if self._refresh_thread(c, conv_id, now) and not created:
                out["changed"] += 1
        for old, into in plan.absorb.items():  # its lines moved above; now its knowledge
            _move_knowledge(c, old, into)
            _delete_conversation(c, old)
            self._mark_changed(c, into, now)
            out["removed"] += 1
        gone = {o["id"] for o in olds} | {x.thread for x in lines if x.thread}
        for conv_id in gone - set(kept) - set(plan.absorb):
            if c.execute("SELECT 1 FROM segments WHERE conversation_id = ? LIMIT 1",
                         (conv_id,)).fetchone() is None:  # every message of it was unsent
                self._drop_thread(c, conv_id, now)
                out["removed"] += 1
        return out

    def _refresh_thread(self, c: sqlite3.Connection, conv_id: str, now: float) -> bool:
        """After a thread's lines changed: its times, search entry and summary state.
        Returns False when its lines are exactly as they were (nothing is touched)."""
        rows = c.execute("SELECT id, text, speaker FROM segments WHERE conversation_id = ?"
                         " ORDER BY started_at, id", (conv_id,)).fetchall()
        digest = hashlib.sha256(json.dumps([list(r) for r in rows],
                                           ensure_ascii=False).encode()).hexdigest()
        old = c.execute("SELECT lines_hash FROM text_threads WHERE conversation_id = ?",
                        (conv_id,)).fetchone()
        if old is not None and old[0] == digest:
            return False
        c.execute("UPDATE text_threads SET lines_hash = ? WHERE conversation_id = ?",
                  (digest, conv_id))
        c.execute("UPDATE conversations SET"
                  " started_at = (SELECT MIN(started_at) FROM segments WHERE conversation_id = ?),"
                  " ended_at = (SELECT MAX(ended_at) FROM segments WHERE conversation_id = ?)"
                  " WHERE id = ?", (conv_id, conv_id, conv_id))
        self._mark_changed(c, conv_id, now)
        self._reindex(c, conv_id)
        return True

    def _mark_changed(self, c: sqlite3.Connection, conv_id: str, now: float) -> None:
        """A thread's lines changed: summarize it again (it keeps the ids of facts and
        to-dos said again), and take down the digests that covered its old summary."""
        row = c.execute("SELECT status FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            return
        if row["status"] == "summarized":
            _take_down_digests(c, conv_id, now)
        c.execute("UPDATE conversations SET status = 'pending', error = NULL,"
                  " discard_reason = NULL, updated_at = ? WHERE id = ?", (now, conv_id))

    def _reindex(self, c: sqlite3.Connection, conv_id: str) -> None:
        c.execute("DELETE FROM search_fts WHERE kind = 'transcript' AND conversation_id = ?",
                  (conv_id,))
        self._index_transcript(c, conv_id)  # type: ignore[attr-defined]

    def _drop_thread(self, c: sqlite3.Connection, conv_id: str, now: float) -> None:
        """Delete a thread's conversation with its to-dos and facts; a fact said elsewhere
        too moves there, and an erased fact's sentence stays (never served) so a message
        saying it again is stored forgotten."""
        before = _knowledge(c, conv_id)
        self._clear_knowledge(c, conv_id)  # type: ignore[attr-defined]
        _carry_over(c, conv_id, before, [])
        c.execute("INSERT OR IGNORE INTO erased_facts SELECT id, fact, erased_at FROM facts"
                  " WHERE conversation_id = ? AND erased_at IS NOT NULL", (conv_id,))
        _take_down_digests(c, conv_id, now)
        _delete_conversation(c, conv_id)

    # ------------------------------------------------------------- delete chat
    def delete_text_chat(self, source: str, chat_key: str,
                         now: float | None = None) -> dict[str, Any] | None:
        """Remove a chat (``DELETE /v1/ingest/messages/chats/{source}/{chat}``): its
        messages, threads, their facts and to-dos. None when there is no such chat."""
        now = time.time() if now is None else now
        with self.tx() as c:  # type: ignore[attr-defined]
            row = c.execute("SELECT id FROM text_chats WHERE source = ? AND chat_key = ?",
                            (source, chat_key)).fetchone()
            if row is None:
                return None
            convs = [r[0] for r in c.execute("SELECT conversation_id FROM text_threads WHERE"
                                             " chat_id = ?", (row["id"],)).fetchall()]
            for conv_id in convs:
                self._drop_thread(c, conv_id, now)
            messages = c.execute("SELECT COUNT(*) FROM text_messages WHERE chat_id = ?",
                                 (row["id"],)).fetchone()[0]
            c.execute("DELETE FROM sessions WHERE id = ?", (session_id(row["id"]),))
            c.execute("DELETE FROM text_chats WHERE id = ?", (row["id"],))
        self.compact()  # type: ignore[attr-defined]
        return {"threads": len(convs), "messages": messages}

    # ------------------------------------------------------------------ reads
    def texts_for(self, conv_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``{conversation_id: {"source", "chat", "name", "group", "people"}}`` for the
        text threads among ``conv_ids`` (``people``: the other participants' names)."""
        with self._lock:  # type: ignore[attr-defined]
            return self._texts_for(self._conn, list(dict.fromkeys(conv_ids)))  # type: ignore[attr-defined]

    def _texts_for(self, c: sqlite3.Connection,
                   conv_ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(conv_ids), 500):
            part = conv_ids[i:i + 500]
            rows = c.execute(
                "SELECT t.conversation_id, ch.id, ch.source, ch.chat_key, ch.name, ch.is_group"
                " FROM text_threads t JOIN text_chats ch ON ch.id = t.chat_id"
                f" WHERE t.conversation_id IN ({','.join('?' * len(part))})", part).fetchall()
            if not rows:
                continue
            people = self._chat_people(c, {r["id"] for r in rows})
            for r in rows:
                out[r["conversation_id"]] = {
                    "source": r["source"], "chat": r["chat_key"], "name": r["name"],
                    "group": bool(r["is_group"]), "people": people.get(r["id"], [])}
        return out

    def _chat_people(self, c: sqlite3.Connection,
                     chat_ids: set[int]) -> dict[int, list[str]]:
        """Each chat's other participants as their lines name them, in name order (one
        query for all the chats)."""
        out: dict[int, set[str]] = defaultdict(set)
        ids = sorted(chat_ids)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in c.execute(
                    "SELECT j.chat_id, j.handle, h.name, h.person_id, p.name AS person,"
                    " p.merged_into FROM text_chat_handles j JOIN text_chats ch"
                    " ON ch.id = j.chat_id LEFT JOIN text_handles h ON h.source = ch.source"
                    " AND h.handle = j.handle LEFT JOIN people p ON p.id = h.person_id"
                    f" WHERE j.chat_id IN ({','.join('?' * len(part))})", part).fetchall():
                out[r["chat_id"]].add(_handle_name(c, r))
        return {k: sorted(v, key=str.lower) for k, v in out.items()}

    def is_text_thread(self, conv_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM text_threads WHERE conversation_id = ?",  # type: ignore[attr-defined]
                         (conv_id,)) is not None

    def person_handles(self) -> dict[int, list[dict[str, str]]]:
        """``{person id: [{"source", "handle"}]}``: the phone numbers and emails each
        person texts from, for linking them to the hub's contacts (list_people)."""
        out: dict[int, list[dict[str, str]]] = defaultdict(list)
        with self._lock:  # type: ignore[attr-defined]
            c = self._conn  # type: ignore[attr-defined]
            for r in c.execute("SELECT h.source, h.handle, h.person_id, p.merged_into FROM"
                               " text_handles h JOIN people p ON p.id = h.person_id"
                               " ORDER BY h.source, h.handle").fetchall():
                pid = r["person_id"]
                if r["merged_into"] is not None:  # rare: follow the merge
                    live = _live_person(c, pid)
                    if live is None:
                        continue
                    pid = live["id"]
                out[pid].append({"source": r["source"], "handle": r["handle"]})
        return dict(out)

    # --------------------------------------------------------------- the queue
    def summary_queue(self, *, now: float, quiet: float, per_pass: int,
                      archive_before: float | None) -> list[str]:
        """The conversations to summarize this pass, in order: every waiting recording
        (oldest first, as before texts existed), then text threads quiet for ``quiet``
        seconds, newest first, at most ``per_pass``. Threads that ended before
        ``archive_before`` and never had a summary are stored without one first
        (``discarded``, reason ``archived``)."""
        if archive_before is not None:
            with self.tx() as c:  # type: ignore[attr-defined]
                c.execute(
                    "UPDATE conversations SET status = 'discarded', discard_reason = 'archived',"
                    " updated_at = ? WHERE status = 'pending' AND title IS NULL"
                    " AND ended_at < ? AND id IN (SELECT conversation_id FROM text_threads)",
                    (now, archive_before))
        recordings = [r["id"] for r in self._q(  # type: ignore[attr-defined]
            "SELECT id FROM conversations WHERE status = 'pending' AND id NOT IN"
            " (SELECT conversation_id FROM text_threads) ORDER BY started_at")]
        texts = [r["id"] for r in self._q(  # type: ignore[attr-defined]
            "SELECT v.id FROM conversations v JOIN text_threads t ON t.conversation_id = v.id"
            " WHERE v.status = 'pending' AND v.ended_at <= ? ORDER BY v.ended_at DESC"
            " LIMIT ?", (now - quiet, per_pass))] if per_pass > 0 else []
        return recordings + texts

    def text_stats(self) -> dict[str, Any]:
        """Counts for ``GET /v1/texts``: chats, messages, threads by status."""
        return {
            "chats": self._one("SELECT COUNT(*) AS n FROM text_chats")["n"],  # type: ignore[attr-defined,index]
            "messages": self._one("SELECT COUNT(*) AS n FROM text_messages"  # type: ignore[attr-defined,index]
                                  " WHERE segment_id IS NOT NULL")["n"],
            "threads": {r["status"]: r["n"] for r in self._q(  # type: ignore[attr-defined]
                "SELECT v.status, COUNT(*) AS n FROM text_threads t JOIN conversations v"
                " ON v.id = t.conversation_id GROUP BY v.status")},
            "last_message_at": self._one(  # type: ignore[attr-defined,index]
                "SELECT MAX(sent_at) AS t FROM text_messages")["t"],
        }
