"""The schema: the frozen first schema (``SCHEMA``) and the ``MIGRATIONS`` registry,
run by ``migrate`` when ``Store`` opens a database."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator

from ..domain import CONVERSATION, SPEECH, is_explicit, person_name, short_title
from .connection import _begin_immediate
from .people import _ensure_person, _person_id, _rename_person

# The first schema (version 1), frozen. A new database starts from it and then runs
# every step of MIGRATIONS, the same steps an old database runs, so both end up with the
# same tables and columns in the same order. Never edit it: a new table, column or index
# is a new step at the end of MIGRATIONS.
SCHEMA = """
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

# Tables and indexes added after the first schema, by the step that adds them. Each
# statement is IF NOT EXISTS: step 17 runs them all again for databases that missed one.

# Step 2: the line-level full-text index for "what did X say about Y".
LINE_INDEX_SCHEMA = """
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
"""

# Step 3: voice samples.
VOICE_SAMPLES_SCHEMA = """
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
"""

# Step 10: people with a stable id.
PEOPLE_SCHEMA = """
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
"""

# Step 17: the tables an older store.py created on every start (CREATE TABLE IF NOT
# EXISTS in its SCHEMA) without a numbered step.
LATER_SCHEMA = """
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

-- "Enroll my voice" from the next recording (athena/voice_enroll.py): one row,
-- its state as JSON, kept after it ends so the hub can show how it went.
CREATE TABLE IF NOT EXISTS voice_enrollment (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
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

-- Written notes (POST /v1/ingest/text): text the owner wrote by hand in the hub, one
-- conversation of kind 'note' on device 'handwriting', one owner line per written line.
-- source_id is the hub's stable id ("hub-note:<id>"); a resend with it replaces the
-- lines. erased_lines holds sha256 fingerprints of lines erase_fact redacted, so a resend
-- of the same words keeps them "[forgotten]".
CREATE TABLE IF NOT EXISTS written_notes (
    source_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    link TEXT NOT NULL,                       -- the note in the hub
    title TEXT,
    written_at REAL NOT NULL,
    text_hash TEXT NOT NULL,
    erased_lines TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS written_notes_conv ON written_notes(conversation_id);
-- Erased facts whose written note was deleted (DELETE /v1/ingest/text): the sentence
-- (never served) stays, so a note that writes it again keeps it "[forgotten]".
CREATE TABLE IF NOT EXISTS erased_facts (
    id INTEGER PRIMARY KEY,                   -- the fact's id
    fact TEXT NOT NULL,
    erased_at REAL NOT NULL
);

-- Meetings (Zoom bot, POST /v1/sessions/{id}/chat): the topic and participant list the bot
-- sends, per session. conversation_id is the meeting's conversation once it has one, so
-- a meeting stays one after retention removed its lines. participants is JSON
-- [{zoom_user_id, name, person_id, joined_at, left_at}] (display name as Zoom showed it).
CREATE TABLE IF NOT EXISTS meeting_info (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    topic TEXT,
    participants TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS meeting_info_conv ON meeting_info(conversation_id);
-- Chat lines already stored, by a SHA-256 of their cleaned fields, so the bot's retries
-- never add a line twice (also after erase_fact redacted it or retention removed it).
CREATE TABLE IF NOT EXISTS meeting_chat (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    line_key TEXT NOT NULL,
    PRIMARY KEY (session_id, line_key)
);

-- Live transcription of a meeting while it runs (meeting_live.py): per track, JSON of
-- what is transcribed so far (lines, whisper context, how far). Work in progress only:
-- deleted once the meeting is processed, so the lines live in segments.
CREATE TABLE IF NOT EXISTS meeting_live (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    track TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, track)
);
-- The worker's per-tick lookups by status (idle sessions, a meeting in progress).
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status);
"""


# Step 16: meeting bots (POST /v1/meetings, athena/meetings/bot/launcher.py).
MEETINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,  -- starting|waiting_room|waiting_permission|recording|ended|failed
    reason TEXT,                        -- why it ended or failed; no_audio while recording
    session_id TEXT NOT NULL,           -- the Athena session the bot uploads to
    meeting_id TEXT NOT NULL,           -- Zoom meeting number (never the passcode)
    title TEXT,
    pid INTEGER,                        -- the bot's process id while it runs
    runner TEXT,                        -- systemd|plain
    unit TEXT,                          -- systemd scope name (systemd runner)
    join_token_kind TEXT,               -- zak|obf|none: how the bot joined
    upload_token_hash TEXT,             -- SHA-256 of the bot's upload token, while active
    stop_requested_at REAL,
    created_at REAL NOT NULL,
    started_at REAL,                    -- first time it recorded
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS meetings_token ON meetings(upload_token_hash);
"""


def _statements(script: str) -> Iterator[str]:
    """The statements of an SQL script, one by one (``executescript`` would commit the
    open transaction first, so migration steps run statements singly)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    if buf.strip() and re.sub(r"--[^\n]*", "", buf).strip():
        raise ValueError(f"incomplete SQL statement: {buf.strip()[:80]!r}")


def _run_script(c: sqlite3.Connection, script: str) -> None:
    for stmt in _statements(script):
        c.execute(stmt)


def _add_column(c: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column unless the table has it already."""
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                     (name,)).fetchone() is not None


# Text messages (POST /v1/ingest/messages; athena/texts/, docs/messages.md). A message is
# a transcript line (segments, kind 'text') plus its receipt here: the source's own id,
# sender handle, edit time. Each chat has one session (id 'text:<chat id>') holding its
# lines; each thread of a chat is a conversation (text_threads). A receipt whose line is
# gone (unsent, pruned by retention) stays, so a retry can't bring the line back.
TEXTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS text_chats (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                     -- imessage (later sms, whatsapp)
    chat_key TEXT NOT NULL,                   -- the source's chat id
    name TEXT,                                -- a group's name
    is_group INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (source, chat_key)
);
-- A sender's handle (phone number or email), the name the owner's contacts give it, and
-- the person it is (people.id): a stable identity the hub links to its contacts by phone
-- number or email (list_people "handles"), whatever the person is called.
CREATE TABLE IF NOT EXISTS text_handles (
    source TEXT NOT NULL,
    handle TEXT NOT NULL,
    name TEXT,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, handle)
);
CREATE TABLE IF NOT EXISTS text_chat_handles (
    chat_id INTEGER NOT NULL REFERENCES text_chats(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    PRIMARY KEY (chat_id, handle)
);
CREATE TABLE IF NOT EXISTS text_messages (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    guid TEXT NOT NULL,                       -- the source's message id
    chat_id INTEGER NOT NULL REFERENCES text_chats(id) ON DELETE CASCADE,
    segment_id INTEGER REFERENCES segments(id) ON DELETE SET NULL,
    sent_at REAL NOT NULL,
    from_me INTEGER NOT NULL DEFAULT 0,
    handle TEXT,                              -- the sender; NULL for the owner
    text_hash TEXT NOT NULL,                  -- of the text and attachments as sent
    edited_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0,       -- unsent: the line is gone for good
    reply_to TEXT,
    attachments TEXT NOT NULL DEFAULT '[]',   -- [{name, type}], never the file
    UNIQUE (source, guid)
);
CREATE INDEX IF NOT EXISTS text_messages_chat ON text_messages(chat_id, sent_at);
CREATE INDEX IF NOT EXISTS text_messages_segment ON text_messages(segment_id);
CREATE INDEX IF NOT EXISTS text_messages_handle ON text_messages(source, handle);
-- A thread of a chat: one conversation. lines_hash is of its messages' ids and texts, so
-- threading again leaves an unchanged thread (and its summary) alone.
CREATE TABLE IF NOT EXISTS text_threads (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL REFERENCES text_chats(id) ON DELETE CASCADE,
    lines_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS text_threads_chat ON text_threads(chat_id);
-- Years of messages: "conversations with X" and the summary queue stay index lookups.
CREATE INDEX IF NOT EXISTS segments_speaker ON segments(speaker COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS conversations_status ON conversations(status, ended_at);
"""


# Each step brings the database from the version before it to its own number
# (MIGRATIONS[n] reaches user_version n). It runs inside the transaction ``migrate``
# opens, so it must not commit: no ``executescript``, no PRAGMAs that need autocommit.

def _m01_first_schema(c: sqlite3.Connection) -> None:
    # a new database: the first schema. A database made by the first store.py has these
    # tables already (it never set user_version, so it is at 0 too).
    _run_script(c, SCHEMA)


def _m02_line_index(c: sqlite3.Connection) -> None:
    # databases created before the line-level index existed
    _run_script(c, LINE_INDEX_SCHEMA)
    c.execute("INSERT INTO segments_fts(segments_fts) VALUES ('rebuild')")


def _m03_voice_samples(c: sqlite3.Connection) -> None:
    # one blended voiceprint per speaker before: keep it as their first sample
    _run_script(c, VOICE_SAMPLES_SCHEMA)
    c.execute(
        "INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
        " SELECT id, embedding, 'legacy', created_at FROM speakers WHERE id NOT IN"
        " (SELECT speaker_id FROM voice_samples)")


def _m04_focused_summaries(c: sqlite3.Connection) -> None:
    # focused summaries: conversation kind, discarded junk, due dates, fact history
    _add_column(c, "conversations", "kind", "TEXT")
    _add_column(c, "conversations", "discard_reason", "TEXT")
    _add_column(c, "action_items", "due_date", "TEXT")  # YYYY-MM-DD
    _add_column(c, "facts", "status", "TEXT NOT NULL DEFAULT 'current'")
    _add_column(c, "facts", "superseded_by", "INTEGER")


def _m05_session_kind(c: sqlite3.Connection) -> None:
    # voice notes: a session is a conversation recording or a voice note
    _add_column(c, "sessions", "kind", f"TEXT NOT NULL DEFAULT '{CONVERSATION}'")


def _m06_calendar_items(c: sqlite3.Connection) -> None:
    # action items for the calendar: kind, time of day (HH:MM local), length,
    # place, when the row was first stored, and whether the user changed them
    _add_column(c, "action_items", "kind", "TEXT NOT NULL DEFAULT 'task'")
    _add_column(c, "action_items", "due_time", "TEXT")
    _add_column(c, "action_items", "duration_minutes", "INTEGER")
    _add_column(c, "action_items", "location", "TEXT")
    _add_column(c, "action_items", "time_unclear", "INTEGER NOT NULL DEFAULT 0")
    _add_column(c, "action_items", "created_at", "REAL")
    _add_column(c, "action_items", "edited", "INTEGER NOT NULL DEFAULT 0")
    c.execute(
        "UPDATE action_items SET created_at = (SELECT started_at FROM conversations"
        " WHERE id = action_items.conversation_id) WHERE created_at IS NULL")


def _m07_list_name(c: sqlite3.Connection) -> None:
    # the to-do list the user named for an item ("add milk to my shopping list")
    _add_column(c, "action_items", "list_name", "TEXT")


def _m08_note_title(c: sqlite3.Connection) -> None:
    # notes from voice notes ("create a note ..."): the note's title
    _add_column(c, "action_items", "note_title", "TEXT")


def _m09_retired_facts(c: sqlite3.Connection) -> None:
    # facts the owner told the hub to forget (status 'retired'): when and why
    _add_column(c, "facts", "retired_at", "REAL")
    _add_column(c, "facts", "retired_reason", "TEXT")


def _m10_people(c: sqlite3.Connection) -> None:
    # people with a stable id: one row per enrolled voice and per name mentioned
    _run_script(c, PEOPLE_SCHEMA)
    for r in c.execute("SELECT id, name, is_owner, created_at FROM speakers"
                       " ORDER BY is_owner DESC, id").fetchall():
        _ensure_person(c, r["name"], r["id"], bool(r["is_owner"]), r["created_at"])
    for r in c.execute(
            "SELECT p.name, MIN(c.started_at) AS first FROM conversation_people p"
            " JOIN conversations c ON c.id = p.conversation_id"
            " GROUP BY p.name ORDER BY first").fetchall():
        _ensure_person(c, r["name"], created_at=r["first"])


def _m11_item_titles(c: sqlite3.Connection) -> None:
    # every action item has a short title, and says whether the user asked for it
    # (a date, time, list, reminder/event/note or a command word) or it is only a
    # suggestion; older rows: their text's start, and explicit by date/list/kind
    _add_column(c, "action_items", "title", "TEXT")
    _add_column(c, "action_items", "explicit", "INTEGER NOT NULL DEFAULT 0")
    for r in map(dict, c.execute("SELECT * FROM action_items").fetchall()):
        c.execute("UPDATE action_items SET title = ?, explicit = ? WHERE id = ?",
                  (short_title(r.get("note_title"), r["text"]),
                   int(is_explicit(r)), r["id"]))


def _m12_possessive_names(c: sqlite3.Connection) -> None:
    # names the summary model saved with a possessive ("Marisol's") become the
    # name ("Marisol"); a person of that name already there absorbs them
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


def _m13_forgetting(c: sqlite3.Connection) -> None:
    # forgetting erases a fact's words (erase_fact); retention prunes old
    # transcripts (CONVO_TRANSCRIPT_RETENTION_DAYS)
    _add_column(c, "facts", "erased_at", "REAL")
    _add_column(c, "segments", "erased", "INTEGER NOT NULL DEFAULT 0")
    _add_column(c, "conversations", "transcript_pruned_at", "REAL")


def _m14_fact_kind(c: sqlite3.Connection) -> None:
    # what sort of fact it is (preference, plan, decision, ...: FACT_KINDS), from
    # the summary model; older facts have none
    _add_column(c, "facts", "kind", "TEXT")


def _m15_chunk_tracks(c: sqlite3.Connection) -> None:
    # chunks per track (a meeting: one track per participant): sequences count per
    # track, so the primary key gains the track; SQLite can't change a primary
    # key, so the table is rebuilt. Older chunks have no track ('').
    if "track" in {r[1] for r in c.execute("PRAGMA table_info(chunks)")}:
        return  # created by an older SCHEMA with the track already
    c.execute(
        "CREATE TABLE chunks_v15 ("
        " session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
        " sequence INTEGER NOT NULL, started_at REAL NOT NULL, duration REAL NOT NULL,"
        " codec TEXT NOT NULL, audio_path TEXT, speech TEXT NOT NULL DEFAULT '[]',"
        " received_at REAL NOT NULL, track TEXT NOT NULL DEFAULT '', track_name TEXT,"
        " PRIMARY KEY (session_id, track, sequence))")
    c.execute(
        "INSERT INTO chunks_v15 (session_id, sequence, started_at, duration, codec,"
        " audio_path, speech, received_at, track, track_name)"
        " SELECT session_id, sequence, started_at, duration, codec, audio_path,"
        " speech, received_at, '', NULL FROM chunks")
    c.execute("DROP TABLE chunks")
    c.execute("ALTER TABLE chunks_v15 RENAME TO chunks")


def _m16_meetings(c: sqlite3.Connection) -> None:
    # meeting bots (POST /v1/meetings): one row per join request. The Zoom
    # passcode is never stored; the bot's upload token only as a SHA-256 hash,
    # cleared when the bot exits.
    _run_script(c, MEETINGS_SCHEMA)


def _m17_unnumbered_changes(c: sqlite3.Connection) -> None:
    # What older versions changed on every start without a step: every table and index
    # added after the first schema (CREATE ... IF NOT EXISTS; a database that has one
    # keeps it unchanged) and two columns. Steps 2, 3, 10 and 16 already created most of
    # these; they are repeated for databases that lack one.
    had_line_index = _has_table(c, "segments_fts")
    for script in (LINE_INDEX_SCHEMA, VOICE_SAMPLES_SCHEMA, PEOPLE_SCHEMA,
                   MEETINGS_SCHEMA, LATER_SCHEMA):
        _run_script(c, script)
    if not had_line_index:
        c.execute("INSERT INTO segments_fts(segments_fts) VALUES ('rebuild')")
    # voices learned from meetings (meetings/people/voices.py): which meeting a sample came
    # from, so processing it again replaces its own samples
    _add_column(c, "voice_samples", "session_id", "TEXT")
    # transcript line kind: speech, or chat (a meeting's chat message); older lines
    # are speech
    _add_column(c, "segments", "kind", f"TEXT NOT NULL DEFAULT '{SPEECH}'")


def _m18_text_messages(c: sqlite3.Connection) -> None:
    # text messages: chats, handles, message receipts, threads; and two indexes that keep
    # person filters and the summary queue fast with years of messages
    _run_script(c, TEXTS_SCHEMA)


# The schema's history, in order: MIGRATIONS[n] brings a database to user_version n.
# Index 0 is an empty database. Add a change only as a new step at the end (AGENTS.md).
MIGRATIONS: list[Callable[[sqlite3.Connection], None] | None] = [
    None,
    _m01_first_schema,
    _m02_line_index,
    _m03_voice_samples,
    _m04_focused_summaries,
    _m05_session_kind,
    _m06_calendar_items,
    _m07_list_name,
    _m08_note_title,
    _m09_retired_facts,
    _m10_people,
    _m11_item_titles,
    _m12_possessive_names,
    _m13_forgetting,
    _m14_fact_kind,
    _m15_chunk_tracks,
    _m16_meetings,
    _m17_unnumbered_changes,
    _m18_text_messages,
]
SCHEMA_VERSION = len(MIGRATIONS) - 1


def migrate(c: sqlite3.Connection) -> int:
    """Run the steps of MIGRATIONS the database at ``c`` (an autocommit connection,
    ``isolation_level=None``) has not had, each in its own transaction that also sets
    ``user_version``: a step that fails leaves the database at the step before, with
    none of its changes. Returns the version reached."""
    # The first schema's tables on every start, as older versions did: a database
    # missing one gets it (CREATE ... IF NOT EXISTS, columns of the first schema only).
    _in_transaction(c, _m01_first_schema)
    version = c.execute("PRAGMA user_version").fetchone()[0]
    for target in range(version + 1, SCHEMA_VERSION + 1):
        _in_transaction(c, _step(target))
    return max(version, SCHEMA_VERSION)


def _step(target: int) -> Callable[[sqlite3.Connection], None]:
    def run(c: sqlite3.Connection) -> None:
        # read again under the write lock: another connection may have run it
        if c.execute("PRAGMA user_version").fetchone()[0] < target:
            step = MIGRATIONS[target]
            assert step is not None
            step(c)
            c.execute(f"PRAGMA user_version = {target}")
    return run


def _in_transaction(c: sqlite3.Connection, work: Callable[[sqlite3.Connection], None]) -> None:
    """Run ``work`` in one write transaction: all of it is committed, or none of it."""
    _begin_immediate(c)
    try:
        work(c)
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")
