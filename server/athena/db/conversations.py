"""Conversations, their summaries, written notes and daily digests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterable
from typing import Any

from ..domain import (
    CHAT,
    CONVERSATION,
    ERASED_TEXT,
    IN_PERSON,
    MEETING,
    NOTE,
    VOICE,
    WRITTEN,
    WRITTEN_DEVICE,
    WRITTEN_SESSION_PREFIX,
    is_explicit,
    short_title,
)
from .carry_over import _carry_over, _insert_fact, _knowledge, _same_item
from .forget import _take_down_digests
from .matching import _ALIKE, _line_key, _line_words, _norm
from .people import _ensure_person


def _move_offsets(c: sqlite3.Connection, conv_id: str, old: list[tuple[float, str]],
                  new: list[str], written_at: float) -> None:
    """A written note was resent: its facts, to-dos and repeats of other facts keep
    pointing at their line (``source_at``) until the re-summary, so links and
    ``erase_fact`` find it. ``old`` is ``(started_at, text)`` of the old lines, ``new`` the
    new lines' text (at ``written_at`` + index). A line found again as it was (else the
    one with most words in common, at least ``_ALIKE`` of them; the nearest of equals)
    gives the new offset; one crossed out gives None, so ``erase_fact`` looks for the
    fact's words instead of redacting the line that is there now."""
    new_norm = [_norm(t) for t in new]
    new_words = [set(t.split()) for t in new_norm]
    moved: dict[float, float | None] = {}

    def move(at: float) -> float | None:
        if at not in moved:
            i = next((k for k, (start, _) in enumerate(old) if at <= start < at + 1), None)
            best: tuple[float, int, int] | None = None
            if i is not None:
                was = _norm(old[i][1])
                words = set(was.split())
                same = [j for j, text in enumerate(new_norm) if text == was]
                for j in same or range(len(new_norm)):
                    # alike: most words in common (cheap for long lines, unlike _same)
                    score = 1.0 if same else (
                        len(words & new_words[j]) / len(words | new_words[j]) if words else 0.0)
                    if score >= _ALIKE and (best is None or (score, -abs(j - i)) > best[:2]):
                        best = (score, -abs(j - i), j)
            moved[at] = written_at + best[2] if best is not None else None
        return moved[at]

    for table, key in (("facts", "id"), ("action_items", "id"),
                       ("fact_sources", "rowid")):
        rows = c.execute(f"SELECT {key}, source_at FROM {table} WHERE conversation_id = ?"
                         " AND source_at IS NOT NULL", (conv_id,)).fetchall()
        c.executemany(f"UPDATE {table} SET source_at = ? WHERE {key} = ?",
                      [(move(r[1]), r[0]) for r in rows])


def _says_words(line: set[str], fact: set[str]) -> bool:
    """A written line (its ``_line_words``) says an erased fact (its ``_line_words``): every
    content word of a fact of up to three ("Alice lives in Berlin"), three quarters and at
    least three of a longer one. "Alice called from the Berlin office" doesn't say it."""
    shared = len(fact & line)
    if len(fact) <= 3:
        return bool(fact) and shared == len(fact)
    return 4 * shared >= 3 * len(fact)


def _delete_conversation(c: sqlite3.Connection, conv_id: str) -> None:
    # its facts go with it (cascade): facts they had replaced become current again,
    # except what a forgotten (retired) one had replaced: an older value of a belief the
    # owner rejected stays replaced, by nothing now (superseded, superseded_by NULL)
    c.execute("UPDATE facts SET status = 'current', superseded_by = NULL"
              " WHERE superseded_by IN (SELECT id FROM facts WHERE conversation_id = ?"
              " AND status != 'retired')", (conv_id,))
    c.execute("UPDATE facts SET superseded_by = NULL WHERE superseded_by IN"
              " (SELECT id FROM facts WHERE conversation_id = ? AND status = 'retired')",
              (conv_id,))
    c.execute("DELETE FROM search_fts WHERE conversation_id = ?", (conv_id,))
    c.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def _move_knowledge(c: sqlite3.Connection, src: str, dst: str) -> None:
    """Give conversation ``src``'s action items, facts and links to ``dst`` (``src`` is
    being merged into it), so the merged conversation's re-summary can carry them over
    (closed action items stay closed, facts keep their links)."""
    for table in ("action_items", "facts", "embeddings"):
        c.execute(f"UPDATE {table} SET conversation_id = ? WHERE conversation_id = ?",
                  (dst, src))
    c.execute("UPDATE OR IGNORE fact_sources SET conversation_id = ? WHERE conversation_id = ?",
              (dst, src))
    c.execute("UPDATE OR IGNORE erased_sources SET conversation_id = ? WHERE conversation_id = ?",
              (dst, src))
    # a fact can't be "said again" in the conversation that now owns it
    c.execute("DELETE FROM fact_sources WHERE conversation_id = ? AND fact_id IN"
              " (SELECT id FROM facts WHERE conversation_id = ?)", (dst, dst))
    c.execute("INSERT OR IGNORE INTO conversation_people SELECT ?, name"
              " FROM conversation_people WHERE conversation_id = ?", (dst, src))
    c.execute("UPDATE search_fts SET conversation_id = ? WHERE conversation_id = ?"
              " AND kind IN ('fact', 'action_item')", (dst, src))


class ConversationsRepo:
    """``Store``'s methods for conversations and notes. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    # ----------------------------------------------------------- conversations
    def overlapping_conversations(self, device: str, start: float, end: float,
                                  gap: float) -> list[dict[str, Any]]:
        """Conversations on ``device`` within ``gap`` seconds of [start, end].

        Voice notes are left out: they are never joined with anything.
        """
        return self._q(
            "SELECT * FROM conversations WHERE device = ? AND ended_at >= ? AND started_at <= ?"
            " AND kind IS NOT ? ORDER BY started_at", (device, start - gap, end + gap, NOTE))

    def session_conversations(self, session_id: str) -> list[str]:
        """Ids of the conversations holding segments of ``session_id``, oldest first."""
        return [r["conversation_id"] for r in self._q(
            "SELECT conversation_id, MIN(started_at) AS first FROM segments"
            " WHERE session_id = ? AND conversation_id IS NOT NULL"
            " GROUP BY conversation_id ORDER BY first", (session_id,))]

    def save_conversation(self, conv_id: str | None, device: str,
                          segment_ids: list[int], absorb: Iterable[str] = (),
                          kind: str | None = None) -> str:
        """Create/extend a conversation from segments; ``absorb`` ids are merged into it.

        ``kind`` is set on a new conversation (voice notes: NOTE); an existing one keeps
        its kind.
        """
        conv_id = conv_id or uuid.uuid4().hex
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO conversations (id, device, started_at, ended_at, updated_at, kind)"
                " VALUES (?, ?, 0, 0, ?, ?) ON CONFLICT(id) DO UPDATE SET status = 'pending',"
                " updated_at = excluded.updated_at", (conv_id, device, now, kind))
            for other in absorb:
                if other == conv_id:
                    continue
                c.execute("UPDATE segments SET conversation_id = ? WHERE conversation_id = ?",
                          (conv_id, other))
                _move_knowledge(c, other, conv_id)
                _delete_conversation(c, other)
            c.executemany("UPDATE segments SET conversation_id = ? WHERE id = ?",
                          [(conv_id, i) for i in segment_ids])
            c.execute(
                "UPDATE conversations SET"
                " started_at = (SELECT MIN(started_at) FROM segments WHERE conversation_id = ?),"
                " ended_at = (SELECT MAX(ended_at) FROM segments WHERE conversation_id = ?)"
                " WHERE id = ?", (conv_id, conv_id, conv_id))
        return conv_id

    def save_summary(self, conv_id: str, summary: dict[str, Any],
                     expect_updated_at: float | None = None) -> list[int] | None:
        """Store structured LLM output and refresh the FTS index for the conversation.

        With ``expect_updated_at`` (the conversation's ``updated_at`` when the summary
        was started) nothing is saved and None is returned if the conversation changed
        meanwhile (a written note resent, a late chunk, a rename) or is gone: it stays
        'pending' and the next pass summarizes what it says now.

        A re-summary (e.g. after a speaker was renamed) keeps what is known about the
        old items: action-item status, fact links and other conversations' sources. A
        fact said again keeps its id (the hub files facts by id), and new facts never take
        an id this conversation's old facts had.
        Returns the ids of the new facts that still need ``link_facts``.
        """
        conv = self.get_conversation(conv_id)
        if conv is None:
            if expect_updated_at is not None:
                return None
            raise KeyError(conv_id)
        fact_ids: list[int] = []
        with self.tx() as c:
            if expect_updated_at is not None:
                now_at = c.execute("SELECT updated_at FROM conversations WHERE id = ?",
                                   (conv_id,)).fetchone()
                if now_at is None or now_at[0] != expect_updated_at:
                    return None
            c.execute(
                "UPDATE conversations SET status = 'summarized', error = NULL, title = ?,"
                " summary = ?, key_points = ?, topics = ?, people = ?, kind = ?,"
                " discard_reason = NULL, updated_at = ? WHERE id = ?",
                (summary["title"], summary["summary"], json.dumps(summary["key_points"]),
                 json.dumps(summary["topics"]), json.dumps(summary["people"]),
                 summary.get("kind"), time.time(), conv_id))
            before = _knowledge(c, conv_id)
            self._clear_knowledge(c, conv_id)
            now = time.time()
            # new items get ids above every old one, as facts do below: SQLite would give
            # a new item the id of an old one that a later item still keeps
            next_item = max([i["id"] for i in before["items"]] + [c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM action_items").fetchone()[0]]) + 1
            for item in summary["action_items"]:
                row = {"id": None, "status": "open", "created_at": now,
                       "kind": item.get("kind") or "task", "due_date": item.get("due_date"),
                       "due_time": item.get("due_time"),
                       "time_unclear": int(bool(item.get("time_unclear"))), "edited": 0,
                       "list_name": item.get("list_name"), "note_title": item.get("note_title"),
                       "title": short_title(item.get("title"), item["text"])}
                row["explicit"] = int(bool(item.get("explicit")) or is_explicit(row))
                row.update(_same_item(before["items"], item))
                if row["id"] is None:
                    row["id"], next_item = next_item, next_item + 1
                cur = c.execute(
                    "INSERT INTO action_items (id, conversation_id, text, owner, due, due_date,"
                    " source_at, status, kind, due_time, duration_minutes, location,"
                    " time_unclear, created_at, edited, list_name, note_title, title,"
                    " explicit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], conv_id, item["text"], item.get("owner"), item.get("due"),
                     row["due_date"], item.get("source_at"), row["status"], row["kind"],
                     row["due_time"], item.get("duration_minutes"), item.get("location"),
                     row["time_unclear"], row["created_at"], row["edited"],
                     row["list_name"], row["note_title"], row["title"],
                     row["explicit"]))
                c.execute("INSERT INTO search_fts VALUES ('action_item', ?, ?, ?)",
                          (str(cur.lastrowid), conv_id, item["text"]))
            # above every old id: SQLite reuses the highest free id, which would give an
            # old fact's id to a different new one
            next_id = max([f["id"] for f in before["facts"]]
                          + [c.execute("SELECT COALESCE(MAX(id), 0) FROM facts").fetchone()[0]]
                          ) + 1
            for fact in summary["facts"]:
                fact_ids.append(_insert_fact(c, conv_id, fact.get("subject"), fact["fact"],
                                             fact.get("source_at"), fact_id=next_id,
                                             kind=fact.get("kind")))
                next_id += 1
            fact_ids = _carry_over(c, conv_id, before, fact_ids)
            c.executemany("INSERT OR IGNORE INTO conversation_people VALUES (?, ?)",
                          [(conv_id, p) for p in summary["people"]])
            for p in summary["people"]:
                _ensure_person(c, p)
            body = "\n".join([summary["title"], summary["summary"], *summary["key_points"],
                              " ".join(summary["topics"]), " ".join(summary["people"])])
            c.execute("INSERT INTO search_fts VALUES ('conversation', ?, ?, ?)",
                      (conv_id, conv_id, body))
            self._index_transcript(c, conv_id)
            # a re-summary of a conversation an erased fact was said in: its new text
            # waits for the forget rewrite too
            # (erased_sources too: the fact may now be owned by another conversation)
            erased = [r[0] for r in c.execute(
                "SELECT fact FROM facts WHERE conversation_id = ? AND erased_at IS NOT NULL"
                " UNION SELECT f.fact FROM fact_sources s JOIN facts f ON f.id = s.fact_id"
                " WHERE s.conversation_id = ? AND f.erased_at IS NOT NULL"
                " UNION SELECT f.fact FROM erased_sources e JOIN facts f ON f.id = e.fact_id"
                " WHERE e.conversation_id = ?", (conv_id, conv_id, conv_id))]
            if erased:
                self._queue_rewrite(c, conv_id, erased, time.time())
        return fact_ids

    def save_discarded(self, conv_id: str, reason: str, kind: str | None) -> None:
        """Mark a conversation as junk (TV, noise): no summary, out of digests.

        The transcript stays and is still searchable; ``POST
        /v1/conversations/{id}/summarize`` (or a speaker rename) tries again.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE conversations SET status = 'discarded', error = NULL, title = NULL,"
                " summary = NULL, key_points = '[]', topics = '[]', people = '[]', kind = ?,"
                " discard_reason = ?, updated_at = ? WHERE id = ?",
                (kind, reason, time.time(), conv_id))
            before = _knowledge(c, conv_id)
            self._clear_knowledge(c, conv_id)
            _carry_over(c, conv_id, before, [])
            self._index_transcript(c, conv_id)

    def _clear_knowledge(self, c: sqlite3.Connection, conv_id: str) -> None:
        # facts of other conversations that this one's facts had replaced become current
        c.execute("UPDATE facts SET status = 'current', superseded_by = NULL"
                  " WHERE superseded_by IN (SELECT id FROM facts WHERE conversation_id = ?)",
                  (conv_id,))
        c.execute("DELETE FROM fact_sources WHERE conversation_id = ?", (conv_id,))
        for table in ("action_items", "facts", "conversation_people"):
            c.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conv_id,))
        c.execute("DELETE FROM search_fts WHERE conversation_id = ?", (conv_id,))
        c.execute("DELETE FROM embeddings WHERE conversation_id = ?", (conv_id,))

    def _index_transcript(self, c: sqlite3.Connection, conv_id: str) -> None:
        transcript = "\n".join(
            f"{r['speaker']}{' (chat)' if r['kind'] == CHAT else ''}: {r['text']}"
            for r in c.execute(
                "SELECT speaker, text, kind FROM segments WHERE conversation_id = ?"
                " ORDER BY started_at", (conv_id,)))
        meeting = self._meetings_for(c, [conv_id]).get(conv_id)
        if meeting:  # "budget meeting" finds the meeting by its topic and people
            transcript = "\n".join([
                f"Online meeting: {meeting['topic'] or ''}",
                "Participants: " + ", ".join(p["name"] for p in meeting["participants"]),
                transcript])
        c.execute("INSERT INTO search_fts VALUES ('transcript', ?, ?, ?)",
                  (conv_id, conv_id, transcript))

    # ------------------------------------------------------------ written notes
    def written_note(self, source_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM written_notes WHERE source_id = ?", (source_id,))

    def summaries_for(self, conv_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``{conversation_id: {"summary", "key_points"}}`` for ``conv_ids`` (a summary
        that erase_fact took down is None, its key points ``[]``)."""
        ids = list(dict.fromkeys(conv_ids))
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._q("SELECT id, summary, key_points FROM conversations"
                             f" WHERE id IN ({','.join('?' * len(part))})", part):
                out[r["id"]] = {"summary": r["summary"],
                                "key_points": json.loads(r["key_points"] or "[]")}
        return out

    def written_notes_for(self, conv_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``{conversation_id: {"source_id", "link", "title"}}`` for the written notes
        among ``conv_ids`` (others are left out)."""
        ids = list(dict.fromkeys(conv_ids))
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._q("SELECT source_id, conversation_id, link, title FROM written_notes"
                             f" WHERE conversation_id IN ({','.join('?' * len(part))})", part):
                out[r["conversation_id"]] = {"source_id": r["source_id"], "link": r["link"],
                                             "title": r["title"]}
        return out

    def put_written_note(self, source_id: str, lines: list[str], written_at: float,
                         link: str, title: str | None, speaker: str,
                         now: float | None = None) -> dict[str, Any]:
        """Store or replace a written note (``POST /v1/ingest/text``).

        One conversation of kind NOTE on ``WRITTEN_DEVICE``; ``lines`` (non-empty, in
        order) become owner lines at ``written_at`` plus one second each, so a fact's
        ``#t=<seconds>`` link points at its line. The conversation is left 'pending' for
        the worker to summarize (the voice-note prompts).

        A second call with the same ``source_id`` and the same lines and ``written_at``
        changes nothing (``link`` and ``title`` are updated): ``"unchanged"``. Other lines
        replace the old ones (``"replaced"``) and the worker summarizes the note again;
        the re-summary keeps the ids of facts and to-dos said again (``save_summary``) and
        drops the others. A line erased by ``erase_fact`` stays "[forgotten]" when the
        same words come back, and a line that says an erased fact (of any conversation,
        ``_says_words``) is stored "[forgotten]" too.
        The digests that covered the note are taken down and written again.
        Returns ``{"result", "conversation_id", "lines", "erased_lines"}``.
        """
        now = time.time() if now is None else now
        text_hash = hashlib.sha256(json.dumps(
            {"lines": lines, "written_at": round(written_at, 3)},
            ensure_ascii=False).encode()).hexdigest()
        session_id = WRITTEN_SESSION_PREFIX + source_id
        with self.tx() as c:
            row = c.execute("SELECT * FROM written_notes WHERE source_id = ?",
                            (source_id,)).fetchone()
            old_conv = row["conversation_id"] if row is not None and c.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (row["conversation_id"],)).fetchone() else None
            if old_conv is not None and row["text_hash"] == text_hash:
                c.execute("UPDATE written_notes SET link = ?, title = ? WHERE source_id = ?",
                          (link, title, source_id))
                return {"result": "unchanged", "conversation_id": old_conv,
                        "lines": len(lines), "erased_lines": c.execute(
                            "SELECT COUNT(*) FROM segments WHERE session_id = ? AND erased = 1",
                            (session_id,)).fetchone()[0]}
            conv_id = old_conv or uuid.uuid4().hex
            erased_keys = json.loads(row["erased_lines"]) if row is not None else []
            # every erased fact, of any conversation: the owner forgot it, so a note that
            # writes it (again, or for the first time in another note) doesn't keep it
            erased_facts = [w for w in (_line_words(r[0]) for r in c.execute(
                "SELECT fact FROM facts WHERE erased_at IS NOT NULL"
                " UNION SELECT fact FROM erased_facts")) if w]
            old_lines: list[tuple[float, str]] = []
            if old_conv is not None:
                old_lines = [(r[0], r[1]) for r in c.execute(
                    "SELECT started_at, text FROM segments WHERE conversation_id = ?"
                    " ORDER BY started_at", (conv_id,))]
                c.execute("DELETE FROM segments WHERE session_id = ?", (session_id,))
                _take_down_digests(c, conv_id, now)
            else:
                c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))  # a stale one
                c.execute("INSERT INTO conversations (id, device, started_at, ended_at,"
                          " updated_at, kind) VALUES (?, ?, ?, ?, ?, ?)",
                          (conv_id, WRITTEN_DEVICE, written_at, written_at, now, NOTE))
            end = written_at + len(lines)
            c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                      " received_at, status, stop_reason, kind)"
                      " VALUES (?, ?, ?, ?, ?, 'done', 'text', ?) ON CONFLICT(id) DO UPDATE"
                      " SET started_at = excluded.started_at,"
                      " last_chunk_at = excluded.last_chunk_at,"
                      " received_at = excluded.received_at",
                      (session_id, WRITTEN_DEVICE, written_at, end, now, NOTE))
            redacted = 0
            stored: list[str] = []
            for i, text in enumerate(lines):
                # a line erase_fact redacted (its fingerprint), or one that says an erased
                # fact; only the first are fingerprinted, so a wrong match isn't kept
                words = _line_words(text)
                erased = (_line_key(text) in erased_keys
                          or any(_says_words(words, f) for f in erased_facts))
                redacted += erased
                stored.append(ERASED_TEXT if erased else text)
                c.execute("INSERT INTO segments (session_id, conversation_id, device,"
                          " started_at, ended_at, speaker, is_owner, text, erased)"
                          " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                          (session_id, conv_id, WRITTEN_DEVICE, written_at + i,
                           written_at + i + 1, speaker, ERASED_TEXT if erased else text,
                           int(erased)))
            if old_lines:
                _move_offsets(c, conv_id, old_lines, stored, written_at)
            c.execute("UPDATE conversations SET started_at = ?, ended_at = ?,"
                      " status = 'pending', error = NULL, transcript_pruned_at = NULL,"
                      " updated_at = ? WHERE id = ?", (written_at, end, now, conv_id))
            c.execute("DELETE FROM search_fts WHERE kind = 'transcript'"
                      " AND conversation_id = ?", (conv_id,))
            self._index_transcript(c, conv_id)
            c.execute("INSERT INTO written_notes (source_id, session_id, conversation_id,"
                      " link, title, written_at, text_hash, erased_lines, created_at,"
                      " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                      " ON CONFLICT(source_id) DO UPDATE SET session_id = excluded.session_id,"
                      " conversation_id = excluded.conversation_id, link = excluded.link,"
                      " title = excluded.title, written_at = excluded.written_at,"
                      " text_hash = excluded.text_hash, erased_lines = excluded.erased_lines,"
                      " updated_at = excluded.updated_at",
                      (source_id, session_id, conv_id, link, title, written_at, text_hash,
                       json.dumps(erased_keys), now, now))
        if old_conv is not None:
            self.compact()  # the old lines' words leave the indexes and the -wal file
        return {"result": "replaced" if old_conv else "created", "conversation_id": conv_id,
                "lines": len(lines), "erased_lines": redacted}

    def delete_written_note(self, source_id: str) -> dict[str, Any] | None:
        """Remove a written note (``DELETE /v1/ingest/text/{source_id}``): its lines, its
        conversation, its to-dos and facts, and the digests that covered it (written again
        by the worker). A fact this note shares with another conversation (said there
        too) stays, owned by that one. The sentence of an erased fact stays in
        ``erased_facts`` (never served), and what a forgotten fact had replaced stays
        replaced. None when there is no such note (idempotent)."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM written_notes WHERE source_id = ?",
                            (source_id,)).fetchone()
            if row is None:
                return None
            conv_id = row["conversation_id"]
            if c.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
                before = _knowledge(c, conv_id)
                self._clear_knowledge(c, conv_id)
                _carry_over(c, conv_id, before, [])  # facts said elsewhere move there
                c.execute("INSERT OR IGNORE INTO erased_facts SELECT id, fact, erased_at"
                          " FROM facts WHERE conversation_id = ? AND erased_at IS NOT NULL",
                          (conv_id,))
                _take_down_digests(c, conv_id, now)
                _delete_conversation(c, conv_id)
            c.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
            c.execute("DELETE FROM written_notes WHERE source_id = ?", (source_id,))
        self.compact()
        return {"conversation_id": conv_id}

    def summaries_pending(self, conv_ids: Iterable[str]) -> bool:
        """Some of ``conv_ids`` wait for their (first or next) summary."""
        ids = list(conv_ids)
        return bool(ids) and self._one(
            "SELECT 1 AS x FROM conversations WHERE status = 'pending' AND id IN"
            f" ({','.join('?' * len(ids))}) LIMIT 1", ids) is not None

    def delete_empty_conversations(self) -> int:
        # A conversation whose transcript retention removed keeps its summary, facts and
        # action items: it has no lines on purpose.
        with self.tx() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM conversations WHERE transcript_pruned_at IS NULL"
                " AND id NOT IN (SELECT DISTINCT conversation_id FROM segments"
                "  WHERE conversation_id IS NOT NULL)")]
            for conv_id in ids:
                _delete_conversation(c, conv_id)
            return len(ids)

    def mark_conversation_failed(self, conv_id: str, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE conversations SET status = 'failed', error = ?, updated_at = ?"
                      " WHERE id = ?", (error, time.time(), conv_id))

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        if row is None:
            return None
        for k in ("key_points", "topics", "people"):
            row[k] = json.loads(row[k])
        row["action_items"] = self._q(
            "SELECT * FROM action_items WHERE conversation_id = ? ORDER BY id", (conv_id,))
        row["facts"] = self._q("SELECT * FROM facts WHERE conversation_id = ?"
                               " AND status != 'retired' ORDER BY id", (conv_id,))
        # erase_fact took its summary down until the worker has rewritten it
        row["forget_pending"] = self.rewrite_pending([conv_id])
        # a note written by hand in the hub: {"source_id", "link", "title"}, else None
        row["written"] = self.written_notes_for([conv_id]).get(conv_id)
        # a Zoom meeting: {"topic", "participants"} (``meetings_for``), else None
        row["meeting"] = self.meetings_for([conv_id]).get(conv_id)
        return row

    def list_conversations(self, *, since: float | None = None, until: float | None = None,
                           person: str | None = None, kind: str | None = None,
                           limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. ``kind``: NOTE = notes (voice and written), VOICE = only voice
        notes, WRITTEN = only notes written by hand, CONVERSATION = everything else,
        MEETING = only online meetings, IN_PERSON = conversations that are no meeting."""
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list[Any] = []
        if kind == NOTE:
            sql += " AND kind = ?"
            params.append(NOTE)
        elif kind == VOICE:
            sql += " AND kind = ? AND device IS NOT ?"
            params += [NOTE, WRITTEN_DEVICE]
        elif kind == WRITTEN:
            sql += " AND device = ?"
            params.append(WRITTEN_DEVICE)
        elif kind == CONVERSATION:
            sql += " AND kind IS NOT ?"
            params.append(NOTE)
        elif kind == MEETING:
            sql += (" AND id IN (SELECT conversation_id FROM meeting_info"
                    " WHERE conversation_id IS NOT NULL)")
        elif kind == IN_PERSON:
            sql += (" AND kind IS NOT ? AND id NOT IN (SELECT conversation_id FROM"
                    " meeting_info WHERE conversation_id IS NOT NULL)")
            params.append(NOTE)
        if since is not None:
            sql += " AND ended_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND started_at <= ?"
            params.append(until)
        if person:
            sql += (" AND (id IN (SELECT conversation_id FROM conversation_people"
                    " WHERE name = ?) OR id IN (SELECT conversation_id FROM segments"
                    " WHERE speaker = ? COLLATE NOCASE))")
            params += [person, person]
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self._q(sql, params)
        written = self.written_notes_for(r["id"] for r in rows)
        meetings = self.meetings_for(r["id"] for r in rows)
        for row in rows:
            for k in ("key_points", "topics", "people"):
                row[k] = json.loads(row[k])
            row["written"] = written.get(row["id"])
            row["meeting"] = meetings.get(row["id"])
        return rows

    def conversations_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM conversations WHERE status = ? ORDER BY started_at", (status,))]

    def failed_conversations(self, before: float) -> list[str]:
        """Failed conversations whose last attempt was at or before ``before``, oldest first."""
        return [r["id"] for r in self._q(
            "SELECT id FROM conversations WHERE status = 'failed' AND updated_at <= ?"
            " ORDER BY updated_at, started_at", (before,))]

    # ----------------------------------------------------------------- digests
    def put_digest(self, date: str, content: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, ?)",
                      (date, json.dumps(content), time.time()))

    def get_digest(self, date: str) -> dict[str, Any] | None:
        row = self._one("SELECT content FROM digests WHERE date = ?", (date,))
        return json.loads(row["content"]) if row else None
