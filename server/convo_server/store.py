"""SQLite storage: the source of truth for sessions, transcripts and knowledge.

All timestamps are UTC epoch seconds (REAL). The API layer formats them as ISO-8601.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

-- One row per person (a speaker or someone named in a conversation): a stable id that
-- survives renames, so the hub can link it to a contact. A rename keeps the id and keeps
-- the old name as an alias; renaming onto an existing person merges the old row into it
-- (merged_into). Names are unique among rows that are not merged.
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    speaker_id INTEGER NULL REFERENCES speakers(id) ON DELETE SET NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    merged_into INTEGER NULL REFERENCES people(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS people_name ON people(name) WHERE merged_into IS NULL;

CREATE TABLE IF NOT EXISTS people_aliases (
    person_id INTEGER NOT NULL REFERENCES people(id),
    alias TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (person_id, alias)
);
CREATE INDEX IF NOT EXISTS people_aliases_alias ON people_aliases(alias);

CREATE TABLE IF NOT EXISTS embeddings (
    kind TEXT NOT NULL,                       -- conversation|fact|action_item
    ref_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

-- Several voice samples per speaker (enrollment windows, named speakers); matching uses
-- the closest few instead of one blended voiceprint. speakers.embedding is their mean.
CREATE TABLE IF NOT EXISTS voice_samples (
    id INTEGER PRIMARY KEY,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    source TEXT NOT NULL,                     -- enrolled|named|legacy
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_samples_speaker ON voice_samples(speaker_id);

-- Every conversation that mentioned a fact again ("same" link), so a fact keeps links
-- to all the transcripts it came from.
CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_at REAL,
    PRIMARY KEY (fact_id, conversation_id)
);

-- Settings changed at run time (the hub's settings page): key -> JSON value. A row
-- overrides the matching CONVO_* setting; no row means the .env value.
CREATE TABLE IF NOT EXISTS module_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Voice embeddings of unnamed speakers ("Speaker 1") per session, so naming a speaker
-- later can also teach the diarizer that voice.
CREATE TABLE IF NOT EXISTS speaker_candidates (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (session_id, label)
);

-- Forgetting (erase_fact): the conversations whose summary, key points and title must be
-- rewritten without an erased fact. The text waits here, out of every read path, until
-- the worker's local model has rewritten it (then the row is deleted).
CREATE TABLE IF NOT EXISTS forget_rewrites (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    facts TEXT NOT NULL DEFAULT '[]',         -- JSON: the erased facts' sentences
    title TEXT,
    summary TEXT,
    key_points TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    queued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
-- Daily digests taken down by a forget, written again once their conversations are
-- rewritten.
CREATE TABLE IF NOT EXISTS forget_digests (
    date TEXT PRIMARY KEY,
    conversation_ids TEXT NOT NULL DEFAULT '[]',
    queued_at REAL NOT NULL
);
-- Which conversations an erased fact was already erased from (its own and every one
-- that said it again), so erasing is idempotent and a new repeat is erased once.
CREATE TABLE IF NOT EXISTS erased_sources (
    fact_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    erased_at REAL NOT NULL,
    PRIMARY KEY (fact_id, conversation_id)
);

-- Line-level full-text index for "what did X say about Y" (kept in sync by triggers).
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, content='segments', content_rowid='id', tokenize = 'porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE OF text ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, conversation_id UNINDEXED, body,
    tokenize = 'porter unicode61'
);
"""


_PLACEHOLDER_SPEAKER = re.compile(r"^(speaker\s*\d+|unknown)$", re.IGNORECASE)

# What an erased transcript line says instead (erase_fact)
ERASED_TEXT = "[forgotten]"

# Session kinds (sessions.kind). A voice note's conversation also gets conversations.kind
# NOTE; ordinary conversations get the kind the summary triage picks.
CONVERSATION = "conversation"
NOTE = "note"
SESSION_KINDS = (CONVERSATION, NOTE)


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


@contextmanager
def _init_lock(db_path: str) -> Iterator[None]:
    """Exclusive file lock next to the database while its schema is set up."""
    if db_path == ":memory:":
        yield
        return
    import fcntl

    with open(db_path + ".init-lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
        # deleted rows are overwritten, not left in free pages: a forgotten fact or a
        # pruned transcript must not stay readable in the file
        self._conn.execute("PRAGMA secure_delete = ON")
        # The API server and the MCP server open the same database at the same time
        # (deploy starts both): set up and migrate one process at a time.
        with _init_lock(self.path):
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            # databases created before the line-level index existed
            self._conn.execute("INSERT INTO segments_fts(segments_fts) VALUES ('rebuild')")
            self._conn.execute("PRAGMA user_version = 2")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            # one blended voiceprint per speaker before: keep it as their first sample
            self._conn.execute(
                "INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
                " SELECT id, embedding, 'legacy', created_at FROM speakers WHERE id NOT IN"
                " (SELECT speaker_id FROM voice_samples)")
            self._conn.execute("PRAGMA user_version = 3")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            # focused summaries: conversation kind, discarded junk, due dates, fact history
            self._add_column("conversations", "kind", "TEXT")
            self._add_column("conversations", "discard_reason", "TEXT")
            self._add_column("action_items", "due_date", "TEXT")  # YYYY-MM-DD
            self._add_column("facts", "status", "TEXT NOT NULL DEFAULT 'current'")
            self._add_column("facts", "superseded_by", "INTEGER")
            self._conn.execute("PRAGMA user_version = 4")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            # voice notes: a session is a conversation recording or a voice note
            self._add_column("sessions", "kind", f"TEXT NOT NULL DEFAULT '{CONVERSATION}'")
            self._conn.execute("PRAGMA user_version = 5")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            # action items for the calendar: kind, time of day (HH:MM local), length,
            # place, when the row was first stored, and whether the user changed them
            self._add_column("action_items", "kind", "TEXT NOT NULL DEFAULT 'task'")
            self._add_column("action_items", "due_time", "TEXT")
            self._add_column("action_items", "duration_minutes", "INTEGER")
            self._add_column("action_items", "location", "TEXT")
            self._add_column("action_items", "time_unclear", "INTEGER NOT NULL DEFAULT 0")
            self._add_column("action_items", "created_at", "REAL")
            self._add_column("action_items", "edited", "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "UPDATE action_items SET created_at = (SELECT started_at FROM conversations"
                " WHERE id = action_items.conversation_id) WHERE created_at IS NULL")
            self._conn.execute("PRAGMA user_version = 6")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 7:
            # the to-do list the user named for an item ("add milk to my shopping list")
            self._add_column("action_items", "list_name", "TEXT")
            self._conn.execute("PRAGMA user_version = 7")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 8:
            # notes from voice notes ("create a note ..."): the note's title
            self._add_column("action_items", "note_title", "TEXT")
            self._conn.execute("PRAGMA user_version = 8")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 9:
            # facts the owner told the hub to forget (status 'retired'): when and why
            self._add_column("facts", "retired_at", "REAL")
            self._add_column("facts", "retired_reason", "TEXT")
            self._conn.execute("PRAGMA user_version = 9")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 10:
            # people with a stable id: one row per enrolled voice and per name mentioned
            c = self._conn
            for r in c.execute("SELECT id, name, is_owner, created_at FROM speakers"
                               " ORDER BY is_owner DESC, id").fetchall():
                _ensure_person(c, r["name"], r["id"], bool(r["is_owner"]), r["created_at"])
            for r in c.execute(
                    "SELECT p.name, MIN(c.started_at) AS first FROM conversation_people p"
                    " JOIN conversations c ON c.id = p.conversation_id"
                    " GROUP BY p.name ORDER BY first").fetchall():
                _ensure_person(c, r["name"], created_at=r["first"])
            c.execute("PRAGMA user_version = 10")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 11:
            # every action item has a short title, and says whether the user asked for it
            # (a date, time, list, reminder/event/note or a command word) or it is only a
            # suggestion; older rows: their text's start, and explicit by date/list/kind
            c = self._conn
            self._add_column("action_items", "title", "TEXT")
            self._add_column("action_items", "explicit", "INTEGER NOT NULL DEFAULT 0")
            for r in map(dict, c.execute("SELECT * FROM action_items").fetchall()):
                c.execute("UPDATE action_items SET title = ?, explicit = ? WHERE id = ?",
                          (short_title(r.get("note_title"), r["text"]),
                           int(is_explicit(r)), r["id"]))
            c.execute("PRAGMA user_version = 11")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 12:
            # names the summary model saved with a possessive ("Marisol's") become the
            # name ("Marisol"); a person of that name already there absorbs them
            c = self._conn
            names = {r[0] for r in c.execute(
                "SELECT name FROM conversation_people UNION SELECT name FROM people"
                " WHERE merged_into IS NULL")}
            for old in sorted(n for n in names if person_name(n) != n and person_name(n)):
                new = person_name(old)
                c.execute("UPDATE OR REPLACE conversation_people SET name = ? WHERE name = ?",
                          (new, old))
                for r in c.execute("SELECT id, people FROM conversations WHERE people LIKE ?",
                                   (f"%{old}%",)).fetchall():
                    people = json.loads(r["people"])
                    fixed = list(dict.fromkeys(new if p == old else p for p in people))
                    if fixed != people:
                        c.execute("UPDATE conversations SET people = ? WHERE id = ?",
                                  (json.dumps(fixed), r["id"]))
                if _person_id(c, old) is not None:
                    _rename_person(c, old, new)
            c.execute("PRAGMA user_version = 12")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 13:
            # forgetting erases a fact's words (erase_fact); retention prunes old
            # transcripts (CONVO_TRANSCRIPT_RETENTION_DAYS)
            self._add_column("facts", "erased_at", "REAL")
            self._add_column("segments", "erased", "INTEGER NOT NULL DEFAULT 0")
            self._add_column("conversations", "transcript_pruned_at", "REAL")
            self._conn.execute("PRAGMA user_version = 13")

    def _add_column(self, table: str, column: str, decl: str) -> None:
        cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str) -> Any:
        """A run-time setting's value, or None when it isn't set (use the .env value)."""
        row = self._one("SELECT value FROM module_settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        """Set a run-time setting; None removes it (back to the .env value)."""
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM module_settings WHERE key = ?", (key,))
            else:
                c.execute("INSERT INTO module_settings (key, value, updated_at) VALUES (?, ?, ?)"
                          " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                          " updated_at = excluded.updated_at",
                          (key, json.dumps(value), time.time()))

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
        kind: str = CONVERSATION,
    ) -> dict[str, Any]:
        """Record a chunk and update the session's silence bookkeeping.

        Returns the updated session row. Raises DuplicateChunk on a repeated
        (session, sequence) so device retries are idempotent, and ValueError if the
        session belongs to another device or is of another ``kind``.
        """
        now = time.time() if now is None else now
        end = started_at + duration
        last_speech = started_at + max(e for _, e in speech) if speech else None
        with self.tx() as c:
            sess = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if sess is None:
                c.execute(
                    "INSERT INTO sessions (id, device, started_at, last_chunk_at, last_speech_at,"
                    " received_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, device, started_at, end, last_speech, now, kind),
                )
            elif sess["device"] != device:
                raise ValueError("session_id already belongs to another device")
            elif sess["kind"] != kind:
                raise ValueError(f"session {session_id} is a {sess['kind']} session, "
                                 f"not {kind}")
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
                    # A chunk for a session being processed or already processed sends
                    # it back for another pass (store-and-forward relays upload late).
                    " status = CASE WHEN status IN ('processing','done','failed') THEN 'ended'"
                    " ELSE status END"
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

    def set_session_status(self, session_id: str, status: str, error: str | None = None,
                           *, only_if: str | None = None) -> bool:
        """Set the status; with ``only_if``, only while the session still has that status."""
        sql, params = "UPDATE sessions SET status = ?, error = ? WHERE id = ?", [status, error,
                                                                                session_id]
        if only_if is not None:
            sql += " AND status = ?"
            params.append(only_if)
        with self.tx() as c:
            return c.execute(sql, params).rowcount > 0

    def idle_sessions(self, received_before: float) -> list[str]:
        rows = self._q("SELECT id FROM sessions WHERE status = 'recording' AND received_at < ?",
                       (received_before,))
        return [r["id"] for r in rows]

    def requeue_interrupted(self) -> int:
        """Send sessions left in 'processing' (server stopped mid-run) back to 'ended'."""
        with self.tx() as c:
            return c.execute(
                "UPDATE sessions SET status = 'ended' WHERE status = 'processing'").rowcount

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

    def clear_chunk_audio(self, session_id: str, sequences: Iterable[int] | None = None) -> None:
        """Forget the audio of the given chunks (all chunks of the session if None)."""
        with self.tx() as c:
            if sequences is None:
                c.execute("UPDATE chunks SET audio_path = NULL WHERE session_id = ?",
                          (session_id,))
            else:
                c.executemany(
                    "UPDATE chunks SET audio_path = NULL WHERE session_id = ? AND sequence = ?",
                    [(session_id, seq) for seq in sequences])

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

    def save_summary(self, conv_id: str, summary: dict[str, Any]) -> list[int]:
        """Store structured LLM output and refresh the FTS index for the conversation.

        A re-summary (e.g. after a speaker was renamed) keeps what is known about the
        old items: action-item status, fact links and other conversations' sources. A
        fact said again keeps its id (the hub files facts by id), and new facts never take
        an id this conversation's old facts had.
        Returns the ids of the new facts that still need ``link_facts``.
        """
        conv = self.get_conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        fact_ids: list[int] = []
        with self.tx() as c:
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
            for item in summary["action_items"]:
                row = {"id": None, "status": "open", "created_at": now,
                       "kind": item.get("kind") or "task", "due_date": item.get("due_date"),
                       "due_time": item.get("due_time"),
                       "time_unclear": int(bool(item.get("time_unclear"))), "edited": 0,
                       "list_name": item.get("list_name"),
                       "title": short_title(item.get("title"), item["text"])}
                row["explicit"] = int(bool(item.get("explicit")) or is_explicit(row))
                row.update(_same_item(before["items"], item))
                cur = c.execute(
                    "INSERT INTO action_items (id, conversation_id, text, owner, due, due_date,"
                    " source_at, status, kind, due_time, duration_minutes, location,"
                    " time_unclear, created_at, edited, list_name, note_title, title,"
                    " explicit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], conv_id, item["text"], item.get("owner"), item.get("due"),
                     row["due_date"], item.get("source_at"), row["status"], row["kind"],
                     row["due_time"], item.get("duration_minutes"), item.get("location"),
                     row["time_unclear"], row["created_at"], row["edited"],
                     row["list_name"], item.get("note_title"), row["title"],
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
                                             fact.get("source_at"), fact_id=next_id))
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
            erased = [r[0] for r in c.execute(
                "SELECT fact FROM facts WHERE conversation_id = ? AND erased_at IS NOT NULL"
                " UNION SELECT f.fact FROM fact_sources s JOIN facts f ON f.id = s.fact_id"
                " WHERE s.conversation_id = ? AND f.erased_at IS NOT NULL", (conv_id, conv_id))]
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
            f"{r['speaker']}: {r['text']}" for r in c.execute(
                "SELECT speaker, text FROM segments WHERE conversation_id = ?"
                " ORDER BY started_at", (conv_id,)))
        c.execute("INSERT INTO search_fts VALUES ('transcript', ?, ?, ?)",
                  (conv_id, conv_id, transcript))

    # ------------------------------------------------------------- fact links
    def facts_by_id(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = list(ids)
        if not ids:
            return []
        return self._q(
            "SELECT f.*, c.started_at AS conversation_at FROM facts f JOIN conversations c"
            f" ON c.id = f.conversation_id WHERE f.id IN ({','.join('?' * len(ids))})"
            " ORDER BY f.id", ids)

    def related_facts(self, conv_id: str, facts: list[dict[str, Any]],
                      limit: int = 15) -> list[dict[str, Any]]:
        """Current and retired facts of other conversations that may repeat or conflict
        with ``facts``: same subject, or matching words. Retired facts are offered too, so
        a forgotten fact said again is linked "same" to it and stays hidden."""
        ids: dict[int, None] = {}
        subjects = {(f.get("subject") or "").strip().lower() for f in facts} - {""}
        for subj in subjects:
            for r in self._q("SELECT id FROM facts WHERE status IN ('current', 'retired')"
                             " AND conversation_id != ? AND lower(subject) = ?"
                             " ORDER BY id DESC LIMIT ?", (conv_id, subj, limit)):
                ids[r["id"]] = None
        for f in facts:
            for hit in self.fts_search(f"{f.get('subject') or ''} {f['fact']}", limit=5):
                if hit["kind"] == "fact" and hit["conversation_id"] != conv_id:
                    ids[int(hit["ref_id"])] = None
        # retired facts are out of the search index: match their words here (few rows)
        words = {w for f in facts for w in _norm(f"{f.get('subject') or ''} {f['fact']}").split()
                 if w not in _STOPWORDS}
        for r in self._q("SELECT id, subject, fact FROM facts WHERE status = 'retired'"
                         " AND conversation_id != ?", (conv_id,)):
            if words & set(_norm(f"{r['subject'] or ''} {r['fact']}").split()):
                ids[r["id"]] = None
        rows = [r for r in self.facts_by_id(ids) if r["status"] in ("current", "retired")]
        return rows[:limit]

    def link_fact(self, new_id: int, relation: str, existing_id: int) -> None:
        """Record that fact ``new_id`` repeats ("same") or updates ("updates") a known one.

        "same": the new row is dropped and its conversation becomes another source of
        the known fact; a repeat of a retired fact so stays hidden. "updates": the older
        of the two (by conversation time) is marked superseded by the newer, so
        re-summarizing an old conversation can't overwrite newer knowledge. A newer fact
        that updates a retired one is a new belief and is shown; the retired one stays
        retired.
        """
        audio: list[str] = []
        with self.tx() as c:
            rows = {r["id"]: r for r in c.execute(
                "SELECT f.id, f.conversation_id, f.source_at, f.status, f.erased_at,"
                " c.started_at FROM facts f"
                " JOIN conversations c ON c.id = f.conversation_id WHERE f.id IN (?, ?)",
                (new_id, existing_id))}
            if len(rows) != 2:
                return
            new, old = rows[new_id], rows[existing_id]
            if relation == "same":
                c.execute("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                          (existing_id, new["conversation_id"], new["source_at"]))
                c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?",
                          (str(new_id),))
                c.execute("DELETE FROM facts WHERE id = ?", (new_id,))
                if old["erased_at"] is not None:  # said again after a forget: erase it too
                    self._erase_mentions(c, existing_id, time.time(), audio)
            elif relation == "updates":
                newer, older = (new, old) if new["started_at"] >= old["started_at"] else (old, new)
                if older["status"] == "retired":  # forgotten: not part of any history
                    return
                c.execute("UPDATE facts SET status = 'superseded', superseded_by = ?"
                          " WHERE id = ?", (newer["id"], older["id"]))
                # whatever the older one had replaced now points at the newer one
                c.execute("UPDATE facts SET superseded_by = ? WHERE superseded_by = ?",
                          (newer["id"], older["id"]))
        if relation == "same" and rows[existing_id]["erased_at"] is not None:
            self._after_erase(audio)

    def fact_history(self, fact_id: int) -> list[dict[str, Any]]:
        """Facts this one replaced (newest first), each with its conversation."""
        return self._q(
            "SELECT f.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM facts f JOIN conversations c ON c.id = f.conversation_id"
            " WHERE f.superseded_by = ? ORDER BY c.started_at DESC", (fact_id,))

    def fact_sources(self, fact_id: int) -> list[dict[str, Any]]:
        """Other conversations that said the same fact again, oldest first."""
        return self._q(
            "SELECT s.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM fact_sources s JOIN conversations c ON c.id = s.conversation_id"
            " WHERE s.fact_id = ? ORDER BY c.started_at", (fact_id,))

    def retire_fact(self, fact_id: int, reason: str = "") -> str | None:
        """Forget a fact (the owner said it's wrong or private): status 'retired', out of
        ``list_facts``, search and the conversation's facts. Returns "retired",
        "already" (it was retired before) or None (no such fact).

        It stays retired when its conversation is re-summarized, and a later "same" fact
        becomes another source of it instead of a new fact. Facts it had replaced stay
        replaced (they are older values of a belief the owner rejected).
        """
        with self.tx() as c:
            row = c.execute("SELECT status FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "retired":
                return "already"
            _retire(c, fact_id, time.time(), reason.strip() or None)
        return "retired"

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
                "SELECT k.session_id, k.sequence, k.audio_path FROM chunks k JOIN sessions s"
                " ON s.id = k.session_id WHERE s.status = 'done' AND k.audio_path IS NOT NULL"
                " AND k.started_at + k.duration < ?", (before,)).fetchall()]
            c.executemany("UPDATE chunks SET audio_path = NULL WHERE session_id = ?"
                          " AND sequence = ?", [(r[0], r[1]) for r in audio])
        if convs or audio:
            self.compact()
        return {"conversations": len(convs), "lines": lines, "audio": [r[2] for r in audio]}

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
            lines += self._redact_line(c, conv_id, source_at, audio)
            self._queue_rewrite(c, conv_id, [fact["fact"]], now)
            c.execute("INSERT OR IGNORE INTO erased_sources VALUES (?, ?, ?)",
                      (fact_id, conv_id, now))
            convs += 1
        return {"lines": lines, "conversations": convs}

    def _redact_line(self, c: sqlite3.Connection, conv_id: str, source_at: float | None,
                     audio: list[str]) -> int:
        """Redact the transcript line that starts at ``source_at`` (the summary's line
        offset: whole seconds from the conversation start, so the line starts within a
        second after it). Returns 1 if a line was redacted."""
        if source_at is None:
            return 0
        line = c.execute(
            "SELECT id, session_id, started_at, ended_at FROM segments"
            " WHERE conversation_id = ? AND erased = 0 AND started_at < ? + 1"
            " AND ended_at > ? - 1 ORDER BY abs(started_at - ?) LIMIT 1",
            (conv_id, source_at, source_at, source_at)).fetchone()
        if line is None:
            return 0
        c.execute("UPDATE segments SET text = ?, erased = 1 WHERE id = ?",
                  (ERASED_TEXT, line["id"]))
        c.execute("DELETE FROM search_fts WHERE kind = 'transcript' AND conversation_id = ?",
                  (conv_id,))
        self._index_transcript(c, conv_id)
        for chunk in c.execute(
                "SELECT sequence, audio_path FROM chunks WHERE session_id = ?"
                " AND audio_path IS NOT NULL AND started_at < ? AND started_at + duration > ?",
                (line["session_id"], line["ended_at"], line["started_at"])).fetchall():
            audio.append(chunk["audio_path"])
            c.execute("UPDATE chunks SET audio_path = NULL WHERE session_id = ?"
                      " AND sequence = ?", (line["session_id"], chunk["sequence"]))
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
        for d in c.execute("SELECT date, content FROM digests WHERE content LIKE ?",
                           (f"%{conv_id}%",)).fetchall():
            ids = json.loads(d["content"]).get("conversation_ids", [])
            if conv_id in ids:
                c.execute("INSERT OR REPLACE INTO forget_digests VALUES (?, ?, ?)",
                          (d["date"], json.dumps(ids), now))
                c.execute("DELETE FROM digests WHERE date = ?", (d["date"],))

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

    def search_segments(self, *, query: str | None = None, speaker: str | None = None,
                        since: float | None = None, until: float | None = None,
                        limit: int = 20) -> list[dict[str, Any]]:
        """Transcript lines filtered by speaker/time, ranked by ``query`` if given.

        Without a query, lines come back in chronological order.
        """
        cols = ("s.id, s.conversation_id, s.started_at, s.ended_at, s.speaker, s.is_owner,"
                " s.text, c.title AS conversation_title, c.started_at AS conversation_at,"
                " c.kind AS conversation_kind")
        where = ["s.conversation_id IS NOT NULL"]
        params: list[Any] = []
        if speaker:
            where.append("s.speaker = ? COLLATE NOCASE")
            params.append(speaker)
        if since is not None:
            where.append("s.started_at >= ?")
            params.append(since)
        if until is not None:
            where.append("s.started_at < ?")
            params.append(until)
        q = _fts_query(query or "")
        if q:
            sql = (f"SELECT {cols}, bm25(segments_fts) AS score FROM segments_fts"
                   " JOIN segments s ON s.id = segments_fts.rowid"
                   " JOIN conversations c ON c.id = s.conversation_id"
                   f" WHERE segments_fts MATCH ? AND {' AND '.join(where)}"
                   " ORDER BY score LIMIT ?")
            return self._q(sql, [q, *params, limit])
        sql = (f"SELECT {cols} FROM segments s JOIN conversations c ON c.id = s.conversation_id"
               f" WHERE {' AND '.join(where)} ORDER BY s.started_at LIMIT ?")
        return self._q(sql, [*params, limit])

    def speaker_stats(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT speaker AS name, MAX(is_owner) AS is_owner, COUNT(*) AS lines,"
            " COUNT(DISTINCT conversation_id) AS conversations, MAX(started_at) AS last_heard"
            " FROM segments WHERE conversation_id IS NOT NULL GROUP BY speaker COLLATE NOCASE"
            " ORDER BY last_heard DESC")

    def mentioned_people(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT p.name, COUNT(*) AS conversations, MAX(c.started_at) AS last_mentioned"
            " FROM conversation_people p JOIN conversations c ON c.id = p.conversation_id"
            " GROUP BY p.name ORDER BY last_mentioned DESC")

    def list_facts(self, *, subject: str | None = None, query: str | None = None,
                   limit: int = 50, counts_tz: str | None = None,
                   min_sources: int = 0, min_days: int = 0) -> list[dict[str, Any]]:
        """Current facts matching ``subject``/``query``, newest first.

        A match on an older, replaced fact returns the fact that replaced it. With
        ``counts_tz`` each fact also gets its ``fact_counts`` (local dates in that
        timezone); ``min_sources``/``min_days`` then keep only facts said in at least that
        many conversations on at least that many dates ("confirmed" facts).
        """
        rows = self._list_current_facts(subject, query, -1 if counts_tz else limit)
        if counts_tz is None:
            return rows
        counts = self.fact_counts([r["id"] for r in rows], counts_tz)
        out = []
        for r in rows:
            r.update(counts[r["id"]])
            if r["times_said"] >= min_sources and r["days"] >= min_days:
                out.append(r)
        return out[:limit] if limit >= 0 else out

    def fact_counts(self, ids: Iterable[int], tz: str) -> dict[int, dict[str, Any]]:
        """For each fact: in how many distinct conversations it was said (its own plus
        its ``fact_sources``), on how many distinct local dates (``tz``), and the first
        and last of those dates (YYYY-MM-DD). One query for the whole set."""
        ids = list(ids)
        if not ids:
            return {}
        zone = ZoneInfo(tz)
        marks = ",".join("?" * len(ids))
        said: dict[int, dict[str, str]] = {i: {} for i in ids}
        for r in self._q(
                "SELECT f.id AS fact_id, c.id AS conv, c.started_at FROM facts f"
                f" JOIN conversations c ON c.id = f.conversation_id WHERE f.id IN ({marks})"
                " UNION SELECT s.fact_id, c.id, c.started_at FROM fact_sources s"
                f" JOIN conversations c ON c.id = s.conversation_id WHERE s.fact_id IN ({marks})",
                [*ids, *ids]):
            said[r["fact_id"]][r["conv"]] = (
                datetime.fromtimestamp(r["started_at"], zone).strftime("%Y-%m-%d"))
        out: dict[int, dict[str, Any]] = {}
        for fid, convs in said.items():
            days = sorted(set(convs.values()))
            out[fid] = {"times_said": len(convs), "days": len(days),
                        "first_said": days[0] if days else None,
                        "last_said": days[-1] if days else None}
        return out

    def known_names(self) -> tuple[set[str], set[str]]:
        """(owner names, person names), lower-cased: enrolled speakers marked as the
        owner, and every enrolled speaker or person named in a conversation."""
        owners: set[str] = set()
        people: set[str] = set()
        for r in self._q("SELECT name, is_owner FROM speakers"
                         " UNION SELECT name, 0 FROM conversation_people"
                         " UNION SELECT name, 0 FROM people"
                         " UNION SELECT alias, 0 FROM people_aliases"):
            name = (r["name"] or "").strip().lower()
            people.add(name)
            if r["is_owner"]:
                owners.add(name)
        return owners - {""}, people - {""}

    def _list_current_facts(self, subject: str | None, query: str | None,
                            limit: int) -> list[dict[str, Any]]:
        sql = "SELECT id, status, superseded_by FROM facts WHERE 1=1"
        params: list[Any] = []
        if subject:
            sql += " AND subject LIKE ? COLLATE NOCASE"
            params.append(f"%{subject}%")
        if query:
            ids = [r["ref_id"] for r in self.fts_search(query, limit=200) if r["kind"] == "fact"]
            if not ids:
                return []
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += [int(i) for i in ids]
        current = {r["superseded_by"] if r["status"] == "superseded" and r["superseded_by"]
                   else r["id"] for r in self._q(sql, params)}
        if not current:
            return []
        return self._q(
            "SELECT f.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM facts f JOIN conversations c ON c.id = f.conversation_id"
            f" WHERE f.status = 'current' AND f.id IN ({','.join('?' * len(current))})"
            " ORDER BY c.started_at DESC, f.id LIMIT ?", [*current, limit])

    def put_speaker_candidates(self, session_id: str, centroids: dict[str, np.ndarray]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM speaker_candidates WHERE session_id = ?", (session_id,))
            c.executemany("INSERT INTO speaker_candidates VALUES (?, ?, ?)",
                          [(session_id, label, np.asarray(v, dtype=np.float32).tobytes())
                           for label, v in centroids.items()])

    def rename_speaker(self, conv_id: str, label: str, name: str) -> tuple[int, np.ndarray | None]:
        """Rename ``label`` to ``name`` within one conversation.

        Returns (lines changed, averaged voice embedding of that label if known). The
        conversation is marked pending so the worker re-summarizes it with the real name.
        """
        with self.tx() as c:
            sessions = [r["session_id"] for r in c.execute(
                "SELECT DISTINCT session_id FROM segments WHERE conversation_id = ?"
                " AND speaker = ?", (conv_id, label))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE"
                          " conversation_id = ? AND speaker = ?",
                          (name, _is_owner(c, name), conv_id, label)).rowcount
            vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in c.execute(
                f"SELECT embedding FROM speaker_candidates WHERE label = ? AND session_id IN"
                f" ({','.join('?' * len(sessions))})", (label, *sessions))] if sessions else []
            if n:
                _touch_conversations(c, [conv_id], name)
            _prune_candidates(c, sessions)
        return n, (np.mean(vecs, axis=0) if vecs else None)

    def unnamed_candidates(self) -> list[dict[str, Any]]:
        """Unnamed speakers ("Speaker N") with a stored voice, per session, still in use."""
        rows = self._q(
            "SELECT k.session_id, k.label, k.embedding, COUNT(s.id) AS lines,"
            " GROUP_CONCAT(DISTINCT s.conversation_id) AS conversations,"
            " MIN(s.started_at) AS first_at"
            " FROM speaker_candidates k JOIN segments s ON s.session_id = k.session_id"
            " AND s.speaker = k.label AND s.conversation_id IS NOT NULL"
            " GROUP BY k.session_id, k.label ORDER BY first_at DESC")
        for r in rows:
            r["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            r["conversations"] = (r["conversations"] or "").split(",")
        return rows

    def rename_session_label(self, session_id: str, label: str, name: str) -> tuple[int, list[str]]:
        """Rename an unnamed label within one recording session (all its conversations)."""
        with self.tx() as c:
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM segments WHERE session_id = ?"
                " AND speaker = ? AND conversation_id IS NOT NULL", (session_id, label))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE session_id = ?"
                          " AND speaker = ?", (name, _is_owner(c, name), session_id,
                                               label)).rowcount
            _touch_conversations(c, convs, name)
            _prune_candidates(c, [session_id])
        return n, convs

    def rename_everywhere(self, old: str, new: str) -> tuple[int, list[str]]:
        """Rename a speaker in every conversation and merge their voice into ``new``.

        Returns (lines changed, conversation ids). Unnamed "Speaker N" labels are
        per recording, so callers should only pass real names. The person keeps their id
        (``old`` becomes an alias); if ``new`` is already a person, the old person is
        merged into that one, and the id of the name that already existed survives.
        """
        with self.tx() as c:
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM segments WHERE speaker = ?"
                " COLLATE NOCASE AND conversation_id IS NOT NULL", (old,))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE speaker = ?"
                          " COLLATE NOCASE", (new, _is_owner(c, new, old), old)).rowcount
            mentioned = c.execute("UPDATE OR REPLACE conversation_people SET name = ?"
                                  " WHERE name = ?", (new, old)).rowcount
            src = c.execute("SELECT id, is_owner FROM speakers WHERE name = ? COLLATE NOCASE",
                            (old,)).fetchone()
            dst = c.execute("SELECT id FROM speakers WHERE name = ?", (new,)).fetchone()
            if src is not None and (dst is None or dst["id"] == src["id"]):
                c.execute("UPDATE speakers SET name = ? WHERE id = ?", (new, src["id"]))
            elif src is not None:  # both known: the old voice samples join the new name
                c.execute("UPDATE voice_samples SET speaker_id = ? WHERE speaker_id = ?",
                          (dst["id"], src["id"]))
                if src["is_owner"]:
                    c.execute("UPDATE speakers SET is_owner = 1 WHERE id = ?", (dst["id"],))
                c.execute("UPDATE people SET speaker_id = NULL WHERE speaker_id = ?",
                          (src["id"],))
                c.execute("DELETE FROM speakers WHERE id = ?", (src["id"],))
                _refresh_voiceprint(c, dst["id"])
            if n or mentioned or src is not None:
                _rename_person(c, old, new)
            _touch_conversations(c, convs, None)
        return n, convs

    def get_segment(self, segment_id: int) -> Segment | None:
        row = self._one("SELECT * FROM segments WHERE id = ?", (segment_id,))
        return _segment(row) if row else None

    def reassign_segment(self, segment_id: int, name: str) -> Segment | None:
        """Give one transcript line to ``name``; returns the line as it was before."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE id = ?",
                      (name, _is_owner(c, name), segment_id))
            if row["conversation_id"]:
                _touch_conversations(c, [row["conversation_id"]], name)
            return _segment(dict(row))

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
        row["facts"] = self._q("SELECT * FROM facts WHERE conversation_id = ?"
                               " AND status != 'retired' ORDER BY id", (conv_id,))
        # erase_fact took its summary down until the worker has rewritten it
        row["forget_pending"] = self.rewrite_pending([conv_id])
        return row

    def list_conversations(self, *, since: float | None = None, until: float | None = None,
                           person: str | None = None, kind: str | None = None,
                           limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. ``kind``: NOTE = only voice notes, CONVERSATION = everything else."""
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list[Any] = []
        if kind == NOTE:
            sql += " AND kind = ?"
            params.append(NOTE)
        elif kind == CONVERSATION:
            sql += " AND kind IS NOT ?"
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
        for row in rows:
            for k in ("key_points", "topics", "people"):
                row[k] = json.loads(row[k])
        return rows

    def conversations_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM conversations WHERE status = ? ORDER BY started_at", (status,))]

    def failed_conversations(self, before: float) -> list[str]:
        """Failed conversations whose last attempt was at or before ``before``, oldest first."""
        return [r["id"] for r in self._q(
            "SELECT id FROM conversations WHERE status = 'failed' AND updated_at <= ?"
            " ORDER BY updated_at, started_at", (before,))]

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
            n = c.execute(f"UPDATE action_items SET {', '.join(f'{k} = ?' for k in sets)}"
                          " WHERE id = ?", (*sets.values(), item_id)).rowcount
            if n:
                r = c.execute("SELECT kind, due_date, due_time, list_name FROM action_items"
                              " WHERE id = ?", (item_id,)).fetchone()
                if is_explicit(dict(r)):
                    c.execute("UPDATE action_items SET explicit = 1 WHERE id = ?", (item_id,))
        return self.get_action_item(item_id) if n else None

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
    def add_voice_sample(self, name: str, embedding: np.ndarray, source: str = "named") -> None:
        """Teach the diarizer a voice: add a sample to a speaker (created if new)."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM speakers WHERE name = ?", (name,)).fetchone()
            if row is None:
                vec = _unit(embedding)
                sid = c.execute(
                    "INSERT INTO speakers (name, is_owner, embedding, created_at)"
                    " VALUES (?, 0, ?, ?)", (name, vec.tobytes(), time.time())).lastrowid
            else:
                sid = row["id"]
            _ensure_person(c, name, sid)
            c.execute("INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
                      " VALUES (?, ?, ?, ?)", (sid, _unit(embedding).tobytes(), source,
                                               time.time()))
            # keep every enrollment sample; drop the oldest learned ones beyond the cap
            c.execute(
                "DELETE FROM voice_samples WHERE id IN (SELECT id FROM voice_samples"
                " WHERE speaker_id = ? AND source != 'enrolled' ORDER BY created_at DESC,"
                " id DESC LIMIT -1 OFFSET ?)", (sid, MAX_LEARNED_SAMPLES))
            _refresh_voiceprint(c, sid)

    def put_speaker(self, name: str, embedding: np.ndarray, is_owner: bool,
                    samples: list[np.ndarray] | None = None, source: str = "enrolled") -> None:
        """Create or update a speaker; ``samples`` replace their earlier ``source`` samples."""
        samples = samples if samples is not None else [embedding]
        with self.tx() as c:
            if is_owner:
                c.execute("UPDATE speakers SET is_owner = 0")
            c.execute(
                "INSERT INTO speakers (name, is_owner, embedding, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET is_owner = excluded.is_owner,"
                " embedding = excluded.embedding",
                (name, int(is_owner), _unit(embedding).tobytes(), time.time()))
            sid = c.execute("SELECT id FROM speakers WHERE name = ?", (name,)).fetchone()["id"]
            pid = _ensure_person(c, name, sid, is_owner)
            if pid is not None and not is_owner:
                c.execute("UPDATE people SET is_owner = 0 WHERE id = ? AND speaker_id = ?",
                          (pid, sid))
            c.execute("DELETE FROM voice_samples WHERE speaker_id = ? AND source = ?",
                      (sid, source))
            now = time.time()
            c.executemany(
                "INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
                " VALUES (?, ?, ?, ?)", [(sid, _unit(v).tobytes(), source, now) for v in samples])
            _refresh_voiceprint(c, sid)

    def speakers(self) -> list[dict[str, Any]]:
        """Enrolled/named voices: name, is_owner, embedding (mean), samples (n, dim)."""
        rows = self._q("SELECT id, name, is_owner, embedding FROM speakers"
                       " ORDER BY is_owner DESC, name")
        samples: dict[int, list[np.ndarray]] = {}
        for r in self._q("SELECT speaker_id, embedding FROM voice_samples ORDER BY id"):
            samples.setdefault(r["speaker_id"], []).append(
                np.frombuffer(r["embedding"], dtype=np.float32))
        for r in rows:
            r["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            r["is_owner"] = bool(r["is_owner"])
            # only samples of the voiceprint's speaker model (see _refresh_voiceprint)
            got = [v for v in samples.get(r.pop("id"), []) if v.size == r["embedding"].size]
            r["samples"] = (np.stack(got) if got
                            else np.zeros((0, r["embedding"].size), np.float32))
        return rows

    def owner_name(self) -> str | None:
        row = self._one("SELECT name FROM speakers WHERE is_owner = 1")
        return row["name"] if row else None

    # ------------------------------------------------------------------ people
    def ensure_person(self, name: str, speaker_id: int | None = None,
                      is_owner: bool = False) -> int | None:
        """The id of the person called ``name`` (name or alias, any case), created if new.

        None for an empty name or an unnamed label ("Speaker 1"). ``speaker_id`` links an
        enrolled voice when the person has none yet; ``is_owner`` makes them the owner.
        """
        with self.tx() as c:
            return _ensure_person(c, name, speaker_id, is_owner)

    def person_id(self, name: str, owner_names: Iterable[str] = ()) -> int | None:
        """The id of the person with this name or alias (any case), following merges.

        A name in ``owner_names`` (the words that mean the owner, see
        ``owner_person_id``) resolves to the owner's person when there is one.
        """
        if (name or "").strip().lower() in _lower(owner_names):
            owner = self.owner_person_id(owner_names)
            if owner is not None:
                return owner
        with self._lock:
            return _person_id(self._conn, name)

    def owner_person_id(self, owner_names: Iterable[str] = ()) -> int | None:
        """The owner's person: the one with the enrolled owner voice; without one, the
        first person whose name or alias is in ``owner_names`` (the configured owner
        name first, then words like "me"). This is the single source of the owner."""
        row = self._one("SELECT id FROM people WHERE is_owner = 1 AND merged_into IS NULL")
        if row is not None:
            return row["id"]
        with self._lock:
            for name in owner_names:
                pid = _person_id(self._conn, name)
                if pid is not None:
                    return pid
        return None

    def conversation_participants(self, conv_ids: Iterable[str],
                                  owner_names: Iterable[str] = ()
                                  ) -> dict[str, list[dict[str, Any]]]:
        """For each conversation, the people in it other than the owner, in order of
        first appearance: named speakers of its lines (by first line), then the people
        its summary lists. Each is ``{"id", "name", "names"}``: the person's id (following
        merges; None for a name with no person row), their current name, and every name
        they appear under here (for matching against text). Unnamed labels ("Speaker 2",
        "Unknown") are skipped, and so is the owner: owner lines, the owner's person (see
        ``owner_person_id``), its aliases, and any name in ``owner_names``. One pass for
        the whole set; names are resolved once each."""
        conv_ids = list(dict.fromkeys(conv_ids))
        out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in conv_ids}
        if not conv_ids:
            return out
        owner_names = tuple(owner_names)
        owner = self.owner_person_id(owner_names)
        marks = ",".join("?" * len(conv_ids))
        rows = self._q(
            "SELECT conversation_id AS conv, speaker AS name, MIN(started_at) AS first,"
            " 0 AS src FROM segments WHERE is_owner = 0 AND conversation_id IN"
            f" ({marks}) GROUP BY conversation_id, speaker COLLATE NOCASE"
            " UNION ALL SELECT conversation_id, name, rowid, 1 FROM conversation_people"
            f" WHERE conversation_id IN ({marks}) ORDER BY 1, 4, 3", [*conv_ids, *conv_ids])
        skip = _lower(owner_names) | {r["name"].strip().lower() for r in self._q(
            "SELECT name FROM speakers WHERE is_owner = 1")}
        if owner is not None:
            skip |= {r["name"].strip().lower() for r in self._q(
                "SELECT name FROM people WHERE id = ? UNION SELECT alias FROM people_aliases"
                " WHERE person_id = ?", (owner, owner))}
        resolved: dict[str, tuple[int | None, str]] = {}
        with self._lock:
            for r in rows:
                key = (r["name"] or "").strip().lower()
                if key in resolved or not key or key in skip or _PLACEHOLDER_SPEAKER.match(key):
                    continue
                pid = _person_id(self._conn, r["name"])
                row = (self._conn.execute("SELECT name FROM people WHERE id = ?", (pid,))
                       .fetchone() if pid is not None else None)
                resolved[key] = (pid, row["name"] if row else r["name"].strip())
        for r in rows:
            key = (r["name"] or "").strip().lower()
            if key not in resolved or resolved[key][0] == owner and owner is not None:
                continue
            pid, name = resolved[key]
            entries = out[r["conv"]]
            same = next((e for e in entries if (e["id"] == pid if pid is not None
                                                else e["name"].lower() == key)), None)
            if same is None:
                entries.append({"id": pid, "name": name, "names": [name]})
                same = entries[-1]
            if r["name"].strip().lower() not in (n.lower() for n in same["names"]):
                same["names"].append(r["name"].strip())
        return out

    def people(self, owner_names: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Every person not merged into another: id, name, aliases, is_owner, has_voice,
        conversations (distinct conversations they spoke in or were named in, under their
        name or an alias), first_at and last_at (start of the first and last of those).

        ``is_owner`` is true for ``owner_person_id(owner_names)`` only; other people
        called by an owner name ("Me" next to an enrolled owner "Sam") are listed as
        that owner (their names become the owner's aliases), not as people of their own.
        """
        owner_names = tuple(owner_names)
        owner = self.owner_person_id(owner_names)
        rows = self._q("SELECT id, name, is_owner, speaker_id IS NOT NULL AS has_voice,"
                       " merged_into FROM people ORDER BY id")
        survivor = {r["id"]: r["merged_into"] for r in rows}
        with self._lock:
            fold = {_person_id(self._conn, n) for n in owner_names} - {None, owner}
        if owner is None:
            fold = set()

        def live(pid: int) -> int:
            for _ in range(len(survivor)):
                if survivor.get(pid) is None:
                    break
                pid = survivor[pid]
            return owner if pid in fold else pid

        out = {r["id"]: {"id": r["id"], "name": r["name"], "aliases": [],
                         "is_owner": r["id"] == owner, "has_voice": bool(r["has_voice"]),
                         "_convs": {}}
               for r in rows if r["merged_into"] is None and r["id"] not in fold}
        by_name: dict[str, int] = {}
        for r in sorted(rows, key=lambda r: r["merged_into"] is None):  # live names win
            by_name[r["name"].lower()] = live(r["id"])
        aliases = [(r["person_id"], r["alias"]) for r in self._q(
            "SELECT person_id, alias FROM people_aliases")]
        aliases += [(r["id"], r["name"]) for r in rows if r["id"] in fold]
        for person_id, alias in sorted(aliases, key=lambda a: a[1].lower()):
            pid = live(person_id)
            by_name.setdefault(alias.lower(), pid)
            if pid in out and alias.lower() not in (
                    a.lower() for a in [out[pid]["name"], *out[pid]["aliases"]]):
                out[pid]["aliases"].append(alias)
        for r in self._q(
                "SELECT s.speaker AS name, c.id, c.started_at FROM segments s"
                " JOIN conversations c ON c.id = s.conversation_id"
                " GROUP BY s.speaker, c.id"
                " UNION SELECT p.name, c.id, c.started_at FROM conversation_people p"
                " JOIN conversations c ON c.id = p.conversation_id"):
            pid = by_name.get((r["name"] or "").strip().lower())
            if pid in out:
                out[pid]["_convs"][r["id"]] = r["started_at"]
        for p in out.values():
            starts = p.pop("_convs").values()
            p["conversations"] = len(starts)
            p["first_at"] = min(starts, default=None)
            p["last_at"] = max(starts, default=None)
        return list(out.values())

MAX_LEARNED_SAMPLES = 40  # per speaker, besides enrollment samples


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _refresh_voiceprint(c: sqlite3.Connection, speaker_id: int) -> None:
    vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in c.execute(
        "SELECT embedding FROM voice_samples WHERE speaker_id = ? ORDER BY id", (speaker_id,))]
    # samples of another speaker model (CONVO_SPEAKER_EMBEDDER changed) have another
    # size: the voiceprint follows the newest sample's model
    vecs = [v for v in vecs if vecs and v.size == vecs[-1].size]
    if vecs:
        c.execute("UPDATE speakers SET embedding = ? WHERE id = ?",
                  (_unit(np.mean(vecs, axis=0)).tobytes(), speaker_id))


def _is_owner(c: sqlite3.Connection, name: str, previous: str | None = None) -> int:
    """1 if ``name`` (or ``previous``, when renaming) is the enrolled owner."""
    row = c.execute("SELECT 1 FROM speakers WHERE is_owner = 1 AND (name = ? OR name = ?)",
                    (name, previous or name)).fetchone()
    return int(row is not None)


def _lower(names: Iterable[str]) -> set[str]:
    return {n.strip().lower() for n in names if n and n.strip()}


def _person_id(c: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a name or alias (any case) to a person id, following ``merged_into``.
    A person who is not merged wins over a merged one, a name over an alias."""
    name = (name or "").strip()
    if not name:
        return None
    row = c.execute(
        "SELECT id, merged_into FROM ("
        " SELECT id, merged_into, 0 AS pri FROM people WHERE name = ?"
        " UNION ALL SELECT p.id, p.merged_into, 1 FROM people_aliases a"
        " JOIN people p ON p.id = a.person_id WHERE a.alias = ?)"
        " ORDER BY merged_into IS NOT NULL, pri, id LIMIT 1", (name, name)).fetchone()
    if row is None:
        return None
    pid, seen = row["id"], set()
    while row is not None and row["merged_into"] is not None and pid not in seen:
        seen.add(pid)
        pid = row["merged_into"]
        row = c.execute("SELECT id, merged_into FROM people WHERE id = ?", (pid,)).fetchone()
    return pid


def _ensure_person(c: sqlite3.Connection, name: str | None, speaker_id: int | None = None,
                   is_owner: bool = False, created_at: float | None = None) -> int | None:
    """Person id for ``name`` (see ``Store.ensure_person``); inserts a row if needed."""
    name = (name or "").strip()
    if not name or _PLACEHOLDER_SPEAKER.match(name):
        return None
    pid = _person_id(c, name)
    if pid is None:
        pid = c.execute("INSERT INTO people (name, created_at) VALUES (?, ?)",
                        (name, created_at if created_at is not None else time.time())
                        ).lastrowid
    if speaker_id is not None:
        c.execute("UPDATE people SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL",
                  (speaker_id, pid))
    if is_owner:
        c.execute("UPDATE people SET is_owner = (id = ?)", (pid,))
    return pid


def _add_alias(c: sqlite3.Connection, pid: int, alias: str | None) -> None:
    alias = (alias or "").strip()
    if alias and not _PLACEHOLDER_SPEAKER.match(alias):
        c.execute("INSERT OR IGNORE INTO people_aliases SELECT id, ? FROM people"
                  " WHERE id = ? AND name != ?", (alias, pid, alias))


def _rename_person(c: sqlite3.Connection, old: str, new: str) -> None:
    """``old`` is now called ``new`` (rename_everywhere): the person keeps their id and
    ``old`` becomes an alias. If ``new`` is already another person, the old person is
    merged into them (``merged_into``, aliases and voice move) and that id survives."""
    src, dst = _person_id(c, old), _person_id(c, new)
    if src is None:  # a name nobody had a row for: it becomes an alias of ``new``
        src = dst if dst is not None else _ensure_person(c, new)
        if src is None:
            return
    target = dst if dst is not None else src
    if target != src:
        before = c.execute("SELECT name, speaker_id, is_owner FROM people WHERE id = ?",
                           (src,)).fetchone()
        c.execute("UPDATE people SET merged_into = ? WHERE id = ? OR merged_into = ?",
                  (target, src, src))
        c.execute("INSERT OR IGNORE INTO people_aliases SELECT ?, alias FROM people_aliases"
                  " WHERE person_id = ?", (target, src))
        c.execute("DELETE FROM people_aliases WHERE person_id = ?", (src,))
        c.execute("UPDATE people SET speaker_id = NULL, is_owner = 0 WHERE id = ?", (src,))
        c.execute("UPDATE people SET speaker_id = COALESCE(speaker_id, ?),"
                  " is_owner = MAX(is_owner, ?) WHERE id = ?",
                  (before["speaker_id"], before["is_owner"], target))
        _add_alias(c, target, before["name"])
    current = c.execute("SELECT name FROM people WHERE id = ?", (target,)).fetchone()["name"]
    c.execute("DELETE FROM people_aliases WHERE person_id = ? AND alias = ?", (target, new))
    c.execute("UPDATE people SET name = ? WHERE id = ?", (new.strip(), target))
    _add_alias(c, target, current)
    _add_alias(c, target, old)


def _touch_conversations(c: sqlite3.Connection, conv_ids: Iterable[str],
                         person: str | None) -> None:
    """Mark conversations for re-summary (speaker names changed); note who took part."""
    now = time.time()
    for conv_id in conv_ids:
        if person and not _PLACEHOLDER_SPEAKER.match(person):
            c.execute("INSERT OR IGNORE INTO conversation_people VALUES (?, ?)",
                      (conv_id, person))
            _ensure_person(c, person)
        c.execute("UPDATE conversations SET status = 'pending', updated_at = ? WHERE id = ?",
                  (now, conv_id))


def _prune_candidates(c: sqlite3.Connection, sessions: Iterable[str]) -> None:
    """Forget unnamed-speaker voices whose label no longer appears in its session."""
    for sid in sessions:
        c.execute("DELETE FROM speaker_candidates WHERE session_id = ? AND label NOT IN"
                  " (SELECT DISTINCT speaker FROM segments WHERE session_id = ?)", (sid, sid))


def _delete_conversation(c: sqlite3.Connection, conv_id: str) -> None:
    # its facts go with it (cascade): facts they had replaced become current again
    c.execute("UPDATE facts SET status = 'current', superseded_by = NULL"
              " WHERE superseded_by IN (SELECT id FROM facts WHERE conversation_id = ?)",
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
    # a fact can't be "said again" in the conversation that now owns it
    c.execute("DELETE FROM fact_sources WHERE conversation_id = ? AND fact_id IN"
              " (SELECT id FROM facts WHERE conversation_id = ?)", (dst, dst))
    c.execute("INSERT OR IGNORE INTO conversation_people SELECT ?, name"
              " FROM conversation_people WHERE conversation_id = ?", (dst, src))
    c.execute("UPDATE search_fts SET conversation_id = ? WHERE conversation_id = ?"
              " AND kind IN ('fact', 'action_item')", (dst, src))


def _segment(r: dict[str, Any]) -> Segment:
    return Segment(started_at=r["started_at"], ended_at=r["ended_at"], text=r["text"],
                   speaker=r["speaker"], is_owner=bool(r["is_owner"]), id=r["id"],
                   session_id=r["session_id"], device=r["device"],
                   conversation_id=r["conversation_id"])


# ------------------------------------------------------------ re-summary carry-over
# A re-summary replaces a conversation's action items and facts. An old and a new item
# are the same when the text is the same, or when they come from the same transcript line
# (source_at within _SAME_LINE seconds) and read alike ("Speaker 1 is vegetarian" ->
# "Alice is vegetarian" after a rename).
_SAME_LINE = 1.0
_ALIKE = 0.6


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", (text or "").lower()).split())


def _same(text_a: str | None, at_a: float | None, text_b: str | None,
          at_b: float | None) -> bool:
    a, b = _norm(text_a), _norm(text_b)
    if a and a == b:
        return True
    return (at_a is not None and at_b is not None and abs(at_a - at_b) <= _SAME_LINE
            and SequenceMatcher(None, a, b).ratio() >= _ALIKE)


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


def _same_item(before: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    """What a re-summarized action item keeps from the matching old one (and takes it
    out of ``before``): its id (the hub files items by id), status, when it was first
    stored, whether it was explicit, and the kind, date, time, list and title if the user
    changed them."""
    # the same text first (a similar item said nearby must not take its row); then
    # closed or edited ones: they are what the user would lose
    text = _norm(item["text"])
    old = next((o for o in sorted(before, key=lambda o: (_norm(o["text"]) != text,
                                                         o["status"] == "open",
                                                         not o["edited"], o["id"]))
                if _same(item["text"], item.get("source_at"), o["text"], o["source_at"])),
               None)
    if old is None:
        return {}
    before.remove(old)
    keep = {"id": old["id"], "status": old["status"],
            "created_at": old["created_at"] or time.time()}
    if old["explicit"]:  # the hub filed it as the user's: it stays that
        keep["explicit"] = 1
    if old["edited"]:
        keep.update({k: old[k] for k in ("kind", "due_date", "due_time", "time_unclear",
                                         "list_name", "title", "edited")})
    return keep


def _insert_fact(c: sqlite3.Connection, conv_id: str, subject: str | None, fact: str,
                 source_at: float | None, fact_id: int | None = None) -> int:
    cur = c.execute("INSERT INTO facts (id, conversation_id, subject, fact, source_at)"
                    " VALUES (?, ?, ?, ?, ?)", (fact_id, conv_id, subject, fact, source_at))
    c.execute("INSERT INTO search_fts VALUES ('fact', ?, ?, ?)",
              (str(cur.lastrowid), conv_id, f"{subject or ''}: {fact}"))
    return int(cur.lastrowid or 0)


def _retire(c: sqlite3.Connection, fact_id: int, at: float | None,
            reason: str | None) -> None:
    """Mark a fact retired and take it out of search (index and embeddings)."""
    c.execute("UPDATE facts SET status = 'retired', superseded_by = NULL, retired_at = ?,"
              " retired_reason = ? WHERE id = ?", (at, reason, fact_id))
    c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?", (str(fact_id),))
    c.execute("DELETE FROM embeddings WHERE kind = 'fact' AND ref_id = ?", (str(fact_id),))


def _knowledge(c: sqlite3.Connection, conv_id: str) -> dict[str, Any]:
    """What a re-summary must not lose: closed action items and the facts' links."""
    facts = [dict(r) for r in c.execute(
        "SELECT id, subject, fact, source_at, status, superseded_by, retired_at,"
        " retired_reason, erased_at FROM facts WHERE conversation_id = ? ORDER BY id",
        (conv_id,))]
    for f in facts:
        f["sources"] = [dict(r) for r in c.execute(
            "SELECT s.conversation_id, s.source_at FROM fact_sources s JOIN conversations v"
            " ON v.id = s.conversation_id WHERE s.fact_id = ? ORDER BY v.started_at",
            (f["id"],))]
        f["replaced"] = [r["id"] for r in c.execute(
            "SELECT id FROM facts WHERE superseded_by = ?", (f["id"],))]
    return {
        "items": [dict(r) for r in c.execute(
            "SELECT id, text, source_at, status, created_at, kind, due_date, due_time,"
            " time_unclear, list_name, title, explicit, edited FROM action_items"
            " WHERE conversation_id = ?"
            " ORDER BY id",
            (conv_id,))],
        "facts": facts,
        # facts of other conversations that this one said again ("same" links)
        "said_again": [dict(r) for r in c.execute(
            "SELECT s.fact_id, s.source_at, f.fact FROM fact_sources s JOIN facts f"
            " ON f.id = s.fact_id WHERE s.conversation_id = ?", (conv_id,))],
    }


def _carry_over(c: sqlite3.Connection, conv_id: str, before: dict[str, Any],
                fact_ids: list[int]) -> list[int]:
    """Re-apply ``before`` (from ``_knowledge``) to the new items; returns the new fact
    ids that are still unlinked."""
    new = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, subject, fact, source_at FROM facts WHERE id IN"
        f" ({','.join('?' * len(fact_ids))})", fact_ids)} if fact_ids else {}
    fresh = [i for i in fact_ids if i in new]

    def match(fact: str, source_at: float | None) -> int | None:
        exact = next((i for i in fresh if _norm(new[i]["fact"]) == _norm(fact)), None)
        return exact or next((i for i in fresh if _same(new[i]["fact"], new[i]["source_at"],
                                                         fact, source_at)), None)

    for old in before["facts"]:
        target = match(old["fact"], old["source_at"])
        sources = old["sources"]
        if target is not None:
            fresh.remove(target)
            # said again: the new row takes the old id, so the hub sees the same fact
            c.execute("UPDATE facts SET id = ? WHERE id = ?", (old["id"], target))
            c.execute("UPDATE search_fts SET ref_id = ? WHERE kind = 'fact' AND ref_id = ?",
                      (str(old["id"]), str(target)))
            target = old["id"]
        elif sources:  # said elsewhere too: keep it, owned by the first other source
            first, sources = sources[0], sources[1:]
            target = _insert_fact(c, first["conversation_id"], old["subject"], old["fact"],
                                  first["source_at"], fact_id=old["id"])
        elif old["status"] == "retired":
            # not said this time: keep the retired row, so a later re-summary that says it
            # again still finds it retired
            target = _insert_fact(c, conv_id, old["subject"], old["fact"], old["source_at"],
                                  fact_id=old["id"])
        else:
            continue
        if old["status"] == "retired":
            _retire(c, target, old["retired_at"], old["retired_reason"])
        if old["erased_at"] is not None:  # erased stays erased, under its new id
            c.execute("UPDATE facts SET erased_at = ? WHERE id = ?", (old["erased_at"], target))
            c.execute("UPDATE OR IGNORE erased_sources SET fact_id = ? WHERE fact_id = ?",
                      (target, old["id"]))
        if old["status"] == "superseded" and old["superseded_by"] and c.execute(
                "SELECT 1 FROM facts WHERE id = ?", (old["superseded_by"],)).fetchone():
            c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                      (old["superseded_by"], target))
        c.executemany("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                      [(target, s["conversation_id"], s["source_at"]) for s in sources])
        if old["replaced"]:
            c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id IN"
                      f" ({','.join('?' * len(old['replaced']))})", (target, *old["replaced"]))
    for said in before["said_again"]:
        hit = match(said["fact"], said["source_at"])
        if hit is not None:  # the same statement again: stays a source, not a new fact
            fresh.remove(hit)
            c.execute("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                      (said["fact_id"], conv_id, new[hit]["source_at"]))
            c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?", (str(hit),))
            c.execute("DELETE FROM facts WHERE id = ?", (hit,))
    return fresh


# Words that say nothing about a topic: common English words plus the ones people use to
# point at a recording ("the one where we talked about ..."). In an OR-query they match
# nearly every conversation and bury the one that is meant.
_STOPWORDS_TEXT = """
a about after again all also am an and any are as at be because been before being but by
can could did do does doing for from had has have having he her here hers him his how i if
in into is it its just me my of on one ones or our out over she so some than that the
their them then there these they this those to too up us very was we were what when where
which while who whom why will with would you your
conversation conversations discussion discussed discussing mentioned mentioning recording
recordings said say saying talk talked talking talks thing things transcript transcripts
"""
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms.

    Stopwords are dropped unless nothing else is left ("the one about AI glasses" ->
    "AI" OR "glasses").
    """
    terms = [t for t in ("".join(ch for ch in w if ch.isalnum()) for w in text.split()) if t]
    topic = [t for t in terms if t.lower() not in _STOPWORDS]
    return " OR ".join(f'"{t}"' for t in topic or terms)
