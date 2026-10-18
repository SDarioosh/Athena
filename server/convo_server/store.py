"""SQLite storage: the source of truth for sessions, transcripts and knowledge.

All timestamps are UTC epoch seconds (REAL). The API layer formats them as ISO-8601.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_chunk_at REAL NOT NULL,        -- wall-clock end of the latest chunk
    last_speech_at REAL,                -- wall-clock end of the latest detected speech
    received_at REAL NOT NULL,          -- server time of the latest upload
    status TEXT NOT NULL DEFAULT 'recording',  -- recording|ended|processing|done|failed
    stop_reason TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    started_at REAL NOT NULL,
    duration REAL NOT NULL,
    codec TEXT NOT NULL,
    audio_path TEXT,                    -- NULL once audio is deleted
    speech TEXT NOT NULL DEFAULT '[]',  -- JSON [[start,end],...] relative to chunk
    received_at REAL NOT NULL,
    PRIMARY KEY (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|summarized|failed
    title TEXT,
    summary TEXT,
    key_points TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    people TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_time ON conversations(started_at);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    device TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    speaker TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_time ON segments(device, started_at);
CREATE INDEX IF NOT EXISTS segments_conv ON segments(conversation_id, started_at);

CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    owner TEXT,
    due TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open|done|dismissed
    source_at REAL
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    subject TEXT,
    fact TEXT NOT NULL,
    source_at REAL
);

CREATE TABLE IF NOT EXISTS conversation_people (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (conversation_id, name)
);

CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,                    -- YYYY-MM-DD (UTC)
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_owner INTEGER NOT NULL DEFAULT 0,
    embedding BLOB NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    kind TEXT NOT NULL,                       -- conversation|fact|action_item
    ref_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, conversation_id UNINDEXED, body,
    tokenize = 'porter unicode61'
);
"""


class DuplicateChunk(Exception):
    pass


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


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._conn.execute(sql, tuple(params)).fetchone())

    # ---------------------------------------------------------------- sessions
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def add_chunk(
        self,
        *,
        session_id: str,
        device: str,
        sequence: int,
        started_at: float,
        duration: float,
        codec: str,
        audio_path: str | None,
        speech: list[tuple[float, float]],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record a chunk and update the session's silence bookkeeping.

        Returns the updated session row. Raises DuplicateChunk on a repeated
        (session, sequence) so device retries are idempotent.
        """
        now = time.time() if now is None else now
        end = started_at + duration
        last_speech = started_at + max(e for _, e in speech) if speech else None
        with self.tx() as c:
            sess = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if sess is None:
                c.execute(
                    "INSERT INTO sessions (id, device, started_at, last_chunk_at, last_speech_at,"
                    " received_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, device, started_at, end, last_speech, now),
                )
            elif sess["device"] != device:
                raise ValueError("session_id already belongs to another device")
            try:
                c.execute(
                    "INSERT INTO chunks (session_id, sequence, started_at, duration, codec,"
                    " audio_path, speech, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, sequence, started_at, duration, codec, audio_path,
                     json.dumps(speech), now),
                )
            except sqlite3.IntegrityError as e:
                raise DuplicateChunk(f"{session_id}#{sequence}") from e
            if sess is not None:
                # Chunks may arrive out of order (offline queues); keep maxima.
                c.execute(
                    "UPDATE sessions SET started_at = MIN(started_at, ?),"
                    " last_chunk_at = MAX(last_chunk_at, ?),"
                    " last_speech_at = CASE WHEN ? IS NULL THEN last_speech_at"
                    "   ELSE MAX(COALESCE(last_speech_at, 0), ?) END,"
                    " received_at = ?,"
                    " status = CASE WHEN status IN ('done','failed') THEN 'ended' ELSE status END"
                    " WHERE id = ?",
                    (started_at, end, last_speech, last_speech, now, session_id),
                )
            return dict(c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def end_session(self, session_id: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET status = 'ended', stop_reason = COALESCE(stop_reason, ?)"
                " WHERE id = ? AND status = 'recording'",
                (reason, session_id),
            )

    def set_session_status(self, session_id: str, status: str, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET status = ?, error = ? WHERE id = ?",
                      (status, error, session_id))

    def idle_sessions(self, received_before: float) -> list[str]:
        rows = self._q("SELECT id FROM sessions WHERE status = 'recording' AND received_at < ?",
                       (received_before,))
        return [r["id"] for r in rows]

    def sessions_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM sessions WHERE status = ? ORDER BY started_at", (status,))]

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._q(
            "SELECT s.*, (SELECT COUNT(*) FROM chunks c WHERE c.session_id = s.id) AS chunk_count"
            " FROM sessions s ORDER BY started_at DESC LIMIT ?", (limit,))

    def chunks(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM chunks WHERE session_id = ? ORDER BY sequence",
                       (session_id,))
        for r in rows:
            r["speech"] = [tuple(s) for s in json.loads(r["speech"])]
        return rows

    def clear_chunk_audio(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE chunks SET audio_path = NULL WHERE session_id = ?", (session_id,))

    # ---------------------------------------------------------------- segments
    def replace_segments(self, session_id: str, device: str, segments: list[Segment],
                         ranges: list[tuple[float, float]]) -> None:
        """Replace the session's segments that start inside the re-transcribed ``ranges``.

        Segments outside those ranges (from chunks whose audio is already deleted)
        are kept, so late chunks for a finished session don't wipe its transcript.
        """
        with self.tx() as c:
            c.executemany(
                "DELETE FROM segments WHERE session_id = ? AND started_at >= ? AND started_at < ?",
                [(session_id, a, b) for a, b in ranges])
            c.executemany(
                "INSERT INTO segments (session_id, device, started_at, ended_at, speaker,"
                " is_owner, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(session_id, device, s.started_at, s.ended_at, s.speaker, int(s.is_owner),
                  s.text) for s in segments],
            )

    def session_segments(self, session_id: str) -> list[Segment]:
        rows = self._q("SELECT * FROM segments WHERE session_id = ? ORDER BY started_at",
                       (session_id,))
        return [_segment(r) for r in rows]

    def segments_for_device(self, device: str, since: float, until: float) -> list[Segment]:
        rows = self._q(
            "SELECT * FROM segments WHERE device = ? AND started_at >= ? AND started_at <= ?"
            " ORDER BY started_at", (device, since, until))
        return [_segment(r) for r in rows]

    def conversation_segments(self, conversation_id: str) -> list[Segment]:
        rows = self._q("SELECT * FROM segments WHERE conversation_id = ? ORDER BY started_at",
                       (conversation_id,))
        return [_segment(r) for r in rows]

    # ----------------------------------------------------------- conversations
    def overlapping_conversations(self, device: str, start: float, end: float,
                                  gap: float) -> list[dict[str, Any]]:
        """Conversations on ``device`` within ``gap`` seconds of [start, end]."""
        return self._q(
            "SELECT * FROM conversations WHERE device = ? AND ended_at >= ? AND started_at <= ?"
            " ORDER BY started_at", (device, start - gap, end + gap))

    def save_conversation(self, conv_id: str | None, device: str,
                          segment_ids: list[int], absorb: Iterable[str] = ()) -> str:
        """Create/extend a conversation from segments; ``absorb`` ids are merged into it."""
        conv_id = conv_id or uuid.uuid4().hex
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO conversations (id, device, started_at, ended_at, updated_at)"
                " VALUES (?, ?, 0, 0, ?) ON CONFLICT(id) DO UPDATE SET status = 'pending',"
                " updated_at = excluded.updated_at", (conv_id, device, now))
            for other in absorb:
                if other == conv_id:
                    continue
                c.execute("UPDATE segments SET conversation_id = ? WHERE conversation_id = ?",
                          (conv_id, other))
                _delete_conversation(c, other)
            c.executemany("UPDATE segments SET conversation_id = ? WHERE id = ?",
                          [(conv_id, i) for i in segment_ids])
            c.execute(
                "UPDATE conversations SET"
                " started_at = (SELECT MIN(started_at) FROM segments WHERE conversation_id = ?),"
                " ended_at = (SELECT MAX(ended_at) FROM segments WHERE conversation_id = ?)"
                " WHERE id = ?", (conv_id, conv_id, conv_id))
        return conv_id

    def save_summary(self, conv_id: str, summary: dict[str, Any]) -> None:
        """Store structured LLM output and refresh the FTS index for the conversation."""
        conv = self.get_conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        with self.tx() as c:
            c.execute(
                "UPDATE conversations SET status = 'summarized', error = NULL, title = ?,"
                " summary = ?, key_points = ?, topics = ?, people = ?, updated_at = ?"
                " WHERE id = ?",
                (summary["title"], summary["summary"], json.dumps(summary["key_points"]),
                 json.dumps(summary["topics"]), json.dumps(summary["people"]), time.time(),
                 conv_id))
            for table in ("action_items", "facts", "conversation_people"):
                c.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conv_id,))
            c.execute("DELETE FROM search_fts WHERE conversation_id = ?", (conv_id,))
            c.execute("DELETE FROM embeddings WHERE conversation_id = ?", (conv_id,))
            for item in summary["action_items"]:
                cur = c.execute(
                    "INSERT INTO action_items (conversation_id, text, owner, due, source_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (conv_id, item["text"], item.get("owner"), item.get("due"),
                     item.get("source_at")))
                c.execute("INSERT INTO search_fts VALUES ('action_item', ?, ?, ?)",
                          (str(cur.lastrowid), conv_id, item["text"]))
            for fact in summary["facts"]:
                cur = c.execute(
                    "INSERT INTO facts (conversation_id, subject, fact, source_at)"
                    " VALUES (?, ?, ?, ?)",
                    (conv_id, fact.get("subject"), fact["fact"], fact.get("source_at")))
                c.execute("INSERT INTO search_fts VALUES ('fact', ?, ?, ?)",
                          (str(cur.lastrowid), conv_id,
                           f"{fact.get('subject') or ''}: {fact['fact']}"))
            c.executemany("INSERT OR IGNORE INTO conversation_people VALUES (?, ?)",
                          [(conv_id, p) for p in summary["people"]])
            body = "\n".join([summary["title"], summary["summary"], *summary["key_points"],
                              " ".join(summary["topics"]), " ".join(summary["people"])])
            c.execute("INSERT INTO search_fts VALUES ('conversation', ?, ?, ?)",
                      (conv_id, conv_id, body))
            transcript = "\n".join(
                f"{r['speaker']}: {r['text']}" for r in c.execute(
                    "SELECT speaker, text FROM segments WHERE conversation_id = ?"
                    " ORDER BY started_at", (conv_id,)))
            c.execute("INSERT INTO search_fts VALUES ('transcript', ?, ?, ?)",
                      (conv_id, conv_id, transcript))

    def delete_empty_conversations(self) -> int:
        with self.tx() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM conversations WHERE id NOT IN"
                " (SELECT DISTINCT conversation_id FROM segments"
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
        row["facts"] = self._q("SELECT * FROM facts WHERE conversation_id = ? ORDER BY id",
                               (conv_id,))
        return row

    def list_conversations(self, *, since: float | None = None, until: float | None = None,
                           person: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql += " AND ended_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND started_at <= ?"
            params.append(until)
        if person:
            sql += (" AND id IN (SELECT conversation_id FROM conversation_people"
                    " WHERE name = ?)")
            params.append(person)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self._q(sql, params)
        for row in rows:
            for k in ("key_points", "topics", "people"):
                row[k] = json.loads(row[k])
        return rows

    def conversations_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM conversations WHERE status = ? ORDER BY started_at", (status,))]

    def list_action_items(self, status: str | None = "open") -> list[dict[str, Any]]:
        sql = ("SELECT a.*, c.title AS conversation_title, c.started_at AS conversation_at"
               " FROM action_items a JOIN conversations c ON c.id = a.conversation_id")
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE a.status = ?"
            params = (status,)
        return self._q(sql + " ORDER BY c.started_at DESC, a.id", params)

    def set_action_item_status(self, item_id: int, status: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE action_items SET status = ? WHERE id = ?",
                             (status, item_id)).rowcount > 0

    # ------------------------------------------------------------------ search
    def fts_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = _fts_query(query)
        if not q:
            return []
        return self._q(
            "SELECT kind, ref_id, conversation_id, snippet(search_fts, 3, '[', ']', '…', 12)"
            " AS snippet, bm25(search_fts) AS score FROM search_fts WHERE search_fts MATCH ?"
            " ORDER BY score LIMIT ?", (q, limit))

    def put_embedding(self, kind: str, ref_id: str, conversation_id: str, model: str,
                      vector: np.ndarray) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?)",
                      (kind, ref_id, conversation_id, model,
                       np.asarray(vector, dtype=np.float32).tobytes()))

    def all_embeddings(self, model: str) -> list[tuple[str, str, str, np.ndarray]]:
        rows = self._q("SELECT kind, ref_id, conversation_id, vector FROM embeddings"
                       " WHERE model = ?", (model,))
        return [(r["kind"], r["ref_id"], r["conversation_id"],
                 np.frombuffer(r["vector"], dtype=np.float32)) for r in rows]

    # ----------------------------------------------------------------- digests
    def put_digest(self, date: str, content: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, ?)",
                      (date, json.dumps(content), time.time()))

    def get_digest(self, date: str) -> dict[str, Any] | None:
        row = self._one("SELECT content FROM digests WHERE date = ?", (date,))
        return json.loads(row["content"]) if row else None

    # ---------------------------------------------------------------- speakers
    def put_speaker(self, name: str, embedding: np.ndarray, is_owner: bool) -> None:
        with self.tx() as c:
            if is_owner:
                c.execute("UPDATE speakers SET is_owner = 0")
            c.execute(
                "INSERT INTO speakers (name, is_owner, embedding, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET is_owner = excluded.is_owner,"
                " embedding = excluded.embedding",
                (name, int(is_owner), np.asarray(embedding, dtype=np.float32).tobytes(),
                 time.time()))

    def speakers(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT name, is_owner, embedding FROM speakers ORDER BY is_owner DESC")
        for r in rows:
            r["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            r["is_owner"] = bool(r["is_owner"])
        return rows


def _delete_conversation(c: sqlite3.Connection, conv_id: str) -> None:
    c.execute("DELETE FROM search_fts WHERE conversation_id = ?", (conv_id,))
    c.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def _segment(r: dict[str, Any]) -> Segment:
    return Segment(started_at=r["started_at"], ended_at=r["ended_at"], text=r["text"],
                   speaker=r["speaker"], is_owner=bool(r["is_owner"]), id=r["id"],
                   session_id=r["session_id"], device=r["device"],
                   conversation_id=r["conversation_id"])


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms."""
    terms = ["".join(ch for ch in t if ch.isalnum()) for t in text.split()]
    return " OR ".join(f'"{t}"' for t in terms if t)
