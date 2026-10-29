"""Forgetting (``erase_fact``), retention and the rewrites they queue."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..domain import CHAT, ERASED_TEXT
from .carry_over import _retire
from .matching import _cited_line, _line_key, _norm


def _take_down_digests(c: sqlite3.Connection, conv_id: str, now: float) -> None:
    """Take down the daily digests that cover ``conv_id``; the worker writes them again
    (``Pipeline.rewrite_forgotten``) once that day's conversations are summarized."""
    for d in c.execute("SELECT date, content FROM digests WHERE content LIKE ?",
                       (f"%{conv_id}%",)).fetchall():
        ids = json.loads(d["content"]).get("conversation_ids", [])
        if conv_id in ids:
            c.execute("INSERT OR REPLACE INTO forget_digests VALUES (?, ?, ?)",
                      (d["date"], json.dumps(ids), now))
            c.execute("DELETE FROM digests WHERE date = ?", (d["date"],))


def _remember_erased_line(c: sqlite3.Connection, conv_id: str, text: str | None) -> None:
    """A line of a written note is being erased: keep its fingerprint, so a resend of the
    note (the hub still has the words) keeps it erased."""
    row = c.execute("SELECT source_id, erased_lines FROM written_notes"
                    " WHERE conversation_id = ?", (conv_id,)).fetchone()
    if row is None or not _norm(text):
        return
    keys = json.loads(row["erased_lines"])
    key = _line_key(text)
    if key not in keys:
        c.execute("UPDATE written_notes SET erased_lines = ? WHERE source_id = ?",
                  (json.dumps(keys + [key]), row["source_id"]))


class ForgetRepo:
    """``Store``'s methods for forgetting and retention. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    # --------------------------------------------------------------- retention
    def prune_transcripts(self, before: float) -> dict[str, Any]:
        """Retention (``CONVO_TRANSCRIPT_RETENTION_DAYS``): delete the transcript lines of
        conversations that ended before ``before``, keeping their title, summary, key
        points, facts and action items, and delete kept audio of finished sessions that
        ended before it. Pending and failed conversations keep their lines (they still
        need them for a summary). Returns ``{"conversations", "lines", "audio"}`` with the
        audio paths deleted from the database (the caller removes the files)."""
        now = time.time()
        with self.tx() as c:
            convs = [r[0] for r in c.execute(
                "SELECT id FROM conversations WHERE ended_at < ? AND transcript_pruned_at"
                " IS NULL AND status NOT IN ('pending', 'failed')", (before,))]
            lines = 0
            for conv_id in convs:
                lines += c.execute("DELETE FROM segments WHERE conversation_id = ?",
                                   (conv_id,)).rowcount
                c.execute("DELETE FROM search_fts WHERE kind = 'transcript'"
                          " AND conversation_id = ?", (conv_id,))
                c.execute("UPDATE conversations SET transcript_pruned_at = ? WHERE id = ?",
                          (now, conv_id))
            audio = [r for r in c.execute(
                "SELECT k.session_id, k.track, k.sequence, k.audio_path FROM chunks k"
                " JOIN sessions s ON s.id = k.session_id WHERE s.status = 'done'"
                " AND k.audio_path IS NOT NULL AND k.started_at + k.duration < ?",
                (before,)).fetchall()]
            c.executemany("UPDATE chunks SET audio_path = NULL WHERE session_id = ?"
                          " AND track = ? AND sequence = ?", [(r[0], r[1], r[2]) for r in audio])
        if convs or audio:
            self.compact()
        return {"conversations": len(convs), "lines": lines, "audio": [r[3] for r in audio]}

    # ------------------------------------------------------------- forgetting
    def erase_fact(self, fact_id: int, reason: str = "") -> dict[str, Any] | None:
        """Forget a fact completely (the owner forgot it in the hub): retired as in
        ``retire_fact``, and its words are erased where they were said, in its own
        conversation and in every one that said it again. None for an unknown id.

        * The transcript line it came from (``source_at``) is redacted in place: its text
          becomes ``ERASED_TEXT``, speaker and times stay, so links keep working. The
          line search index follows (trigger); the conversation's search entry is redone.
        * The conversation's title, summary, key points and topics leave every read path
          now and wait in ``forget_rewrites`` until the worker's model rewrites them
          without the fact (``Pipeline.rewrite_forgotten``). Daily digests that cover the
          conversation are taken down and written again after that.
        * Its embedding and the conversation's are deleted; kept audio of the line
          (``CONVO_KEEP_AUDIO``) is deleted.
        * A later repeat linked "same" to it is erased the same way (``link_fact``).

        Idempotent: a second call erases only what is new and answers ``"already"``.
        Returns ``{"result": "erased"|"already", "lines", "conversations"}``.
        """
        audio: list[str] = []
        with self.tx() as c:
            row = c.execute("SELECT status, erased_at FROM facts WHERE id = ?",
                            (fact_id,)).fetchone()
            if row is None:
                return None
            now = time.time()
            if row["status"] != "retired":
                _retire(c, fact_id, now, reason.strip() or None)
            c.execute("UPDATE facts SET erased_at = COALESCE(erased_at, ?) WHERE id = ?",
                      (now, fact_id))
            done = self._erase_mentions(c, fact_id, now, audio)
        self._after_erase(audio)
        fresh = row["erased_at"] is None or done["conversations"]
        return {"result": "erased" if fresh else "already", **done}

    def _erase_mentions(self, c: sqlite3.Connection, fact_id: int, now: float,
                        audio: list[str]) -> dict[str, int]:
        """Erase an erased fact from each conversation that said it and isn't done yet."""
        fact = c.execute("SELECT conversation_id, source_at, subject, fact FROM facts"
                         " WHERE id = ?", (fact_id,)).fetchone()
        said = [(fact["conversation_id"], fact["source_at"])] + [
            (r["conversation_id"], r["source_at"]) for r in c.execute(
                "SELECT conversation_id, source_at FROM fact_sources WHERE fact_id = ?",
                (fact_id,))]
        done = {r[0] for r in c.execute(
            "SELECT conversation_id FROM erased_sources WHERE fact_id = ?", (fact_id,))}
        lines = convs = 0
        for conv_id, source_at in said:
            if conv_id in done:
                continue
            done.add(conv_id)
            lines += self._redact_line(c, conv_id, source_at, audio, fact["fact"])
            self._queue_rewrite(c, conv_id, [fact["fact"]], now)
            c.execute("INSERT OR IGNORE INTO erased_sources VALUES (?, ?, ?)",
                      (fact_id, conv_id, now))
            convs += 1
        return {"lines": lines, "conversations": convs}

    def _redact_line(self, c: sqlite3.Connection, conv_id: str, source_at: float | None,
                     audio: list[str], fact: str = "") -> int:
        """Redact the transcript line that starts at ``source_at`` (the summary's line
        offset: whole seconds from the conversation start, so the line starts within a
        second after it). Returns 1 if a line was redacted; 0 when that line was already
        redacted (another fact said on it): then no other line is redacted instead.

        A model sometimes cites the wrong line. When the cited line shares no word with
        ``fact`` (or no line is cited) and a line within ``_CITE_SLACK`` seconds shares at
        least two (all of a one-word fact), that line is the one redacted. A cited line
        that only paraphrases the fact, with no line nearby that says its words, is
        still redacted."""
        rows = c.execute(
            "SELECT id, session_id, started_at, ended_at, erased, text, kind FROM segments"
            " WHERE conversation_id = ? ORDER BY started_at", (conv_id,)).fetchall()
        line = _cited_line(rows, source_at, fact)
        if line is None or line["erased"]:
            return 0
        _remember_erased_line(c, conv_id, line["text"])
        c.execute("UPDATE segments SET text = ?, erased = 1 WHERE id = ?",
                  (ERASED_TEXT, line["id"]))
        c.execute("DELETE FROM search_fts WHERE kind = 'transcript' AND conversation_id = ?",
                  (conv_id,))
        self._index_transcript(c, conv_id)
        if line["kind"] == CHAT:  # typed, never heard: no audio holds its words
            return 1
        for chunk in c.execute(
                "SELECT track, sequence, audio_path FROM chunks WHERE session_id = ?"
                " AND audio_path IS NOT NULL AND started_at < ? AND started_at + duration > ?",
                (line["session_id"], line["ended_at"], line["started_at"])).fetchall():
            # every track's audio at that time: a line's words can be heard on another
            # participant's microphone too
            audio.append(chunk["audio_path"])
            c.execute("UPDATE chunks SET audio_path = NULL WHERE session_id = ?"
                      " AND track = ? AND sequence = ?",
                      (line["session_id"], chunk["track"], chunk["sequence"]))
        return 1

    def _queue_rewrite(self, c: sqlite3.Connection, conv_id: str, facts: list[str],
                       now: float) -> None:
        """Take the conversation's title, summary, key points and topics out of every
        read path until they are rewritten without ``facts``; take down its digests."""
        conv = c.execute("SELECT title, summary, key_points, topics FROM conversations"
                         " WHERE id = ?", (conv_id,)).fetchone()
        if conv is None:
            return
        old = c.execute("SELECT facts FROM forget_rewrites WHERE conversation_id = ?",
                        (conv_id,)).fetchone()
        known = json.loads(old["facts"]) if old else []
        facts = list(dict.fromkeys(known + facts))
        if conv["title"] or conv["summary"] or old is None:
            # the text shown now (a first forget, or a re-summary since): rewrite that
            c.execute("INSERT OR REPLACE INTO forget_rewrites (conversation_id, facts, title,"
                      " summary, key_points, topics, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (conv_id, json.dumps(facts), conv["title"], conv["summary"],
                       conv["key_points"], conv["topics"], now))
        else:
            c.execute("UPDATE forget_rewrites SET facts = ?, queued_at = ?, attempts = 0"
                      " WHERE conversation_id = ?", (json.dumps(facts), now, conv_id))
        c.execute("UPDATE conversations SET title = NULL, summary = NULL, key_points = '[]',"
                  " topics = '[]' WHERE id = ?", (conv_id,))
        c.execute("DELETE FROM search_fts WHERE kind = 'conversation' AND conversation_id = ?",
                  (conv_id,))
        c.execute("DELETE FROM embeddings WHERE kind = 'conversation' AND conversation_id = ?",
                  (conv_id,))
        _take_down_digests(c, conv_id, now)

    def _after_erase(self, audio: list[str]) -> None:
        for path in audio:
            Path(path).unlink(missing_ok=True)
        self.compact()

    def compact(self) -> None:
        """After removing text: merge the full-text indexes (a deleted row's words stay
        in older index segments until a merge) and checkpoint the write-ahead log, so the
        old pages leave the -wal file. ``secure_delete`` zeroes freed pages."""
        with self._lock:
            for table in ("segments_fts", "search_fts"):
                self._conn.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def pending_rewrites(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM forget_rewrites ORDER BY attempts, queued_at")
        for r in rows:
            for k in ("facts", "key_points", "topics"):
                r[k] = json.loads(r[k])
        return rows

    def finish_rewrite(self, conv_id: str, queued_at: float, fields: dict[str, Any]) -> bool:
        """Store a conversation's rewritten title, summary, key points and topics and put
        it back in search. False (nothing stored) when it was queued again meanwhile."""
        with self.tx() as c:
            row = c.execute("SELECT queued_at FROM forget_rewrites WHERE conversation_id = ?",
                            (conv_id,)).fetchone()
            if row is None or row["queued_at"] != queued_at:
                return False
            c.execute("DELETE FROM forget_rewrites WHERE conversation_id = ?", (conv_id,))
            conv = c.execute("SELECT status, people FROM conversations WHERE id = ?",
                             (conv_id,)).fetchone()
            if conv is None or conv["status"] != "summarized":
                return True
            c.execute("UPDATE conversations SET title = ?, summary = ?, key_points = ?,"
                      " topics = ?, updated_at = ? WHERE id = ?",
                      (fields["title"], fields["summary"], json.dumps(fields["key_points"]),
                       json.dumps(fields["topics"]), time.time(), conv_id))
            body = "\n".join([fields["title"], fields["summary"], *fields["key_points"],
                              " ".join(fields["topics"]),
                              " ".join(json.loads(conv["people"]))])
            c.execute("DELETE FROM search_fts WHERE kind = 'conversation'"
                      " AND conversation_id = ?", (conv_id,))
            c.execute("INSERT INTO search_fts VALUES ('conversation', ?, ?, ?)",
                      (conv_id, conv_id, body))
        return True

    def drop_rewrite(self, conv_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM forget_rewrites WHERE conversation_id = ?", (conv_id,))

    def rewrite_failed(self, conv_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE forget_rewrites SET attempts = attempts + 1"
                      " WHERE conversation_id = ?", (conv_id,))

    def rewrite_pending(self, conv_ids: Iterable[str]) -> bool:
        ids = list(conv_ids)
        return bool(ids) and self._one(
            "SELECT 1 AS x FROM forget_rewrites WHERE conversation_id IN"
            f" ({','.join('?' * len(ids))}) LIMIT 1", ids) is not None

    def pending_digests(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM forget_digests ORDER BY date")
        for r in rows:
            r["conversation_ids"] = json.loads(r["conversation_ids"])
        return rows

    def drop_pending_digest(self, date: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM forget_digests WHERE date = ?", (date,))
