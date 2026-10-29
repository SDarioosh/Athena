-- An Athena database at user_version 14, for test_schema_snapshot.py.
--
-- Source: the schema a fresh database got from server/convo_server/store.py at commit
-- c2eeff895ec4d34a38ad8a6bcd7f59bdacc5aee4 (main on 2026-10-26, "Merge pull request #113", the last
-- main commit at user_version 14, before tracked chunks (15) and the meetings table (16)).
-- Made by opening a new database with that commit's Store and dumping sqlite_master in
-- creation order, leaving out SQLite's own entries (sqlite_autoindex_*) and the FTS5
-- shadow tables (segments_fts_*, search_fts_*), which the virtual tables create again.
-- Columns added by migrations are where ALTER TABLE put them. Load it with
-- user_version 14. Fictional schema only, no data.

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_chunk_at REAL NOT NULL,        -- wall-clock end of the latest chunk
    last_speech_at REAL,                -- wall-clock end of the latest detected speech
    received_at REAL NOT NULL,          -- server time of the latest upload
    status TEXT NOT NULL DEFAULT 'recording',  -- recording|ended|processing|done|failed
    stop_reason TEXT,
    error TEXT
, kind TEXT NOT NULL DEFAULT 'conversation');

CREATE TABLE chunks (
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

CREATE TABLE conversations (
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
, kind TEXT, discard_reason TEXT, transcript_pruned_at REAL);

CREATE INDEX conversations_time ON conversations(started_at);

CREATE TABLE segments (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    device TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    speaker TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL
, erased INTEGER NOT NULL DEFAULT 0);

CREATE INDEX segments_time ON segments(device, started_at);

CREATE INDEX segments_conv ON segments(conversation_id, started_at);

CREATE TABLE action_items (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    owner TEXT,
    due TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open|done|dismissed
    source_at REAL
, due_date TEXT, kind TEXT NOT NULL DEFAULT 'task', due_time TEXT, duration_minutes INTEGER, location TEXT, time_unclear INTEGER NOT NULL DEFAULT 0, created_at REAL, edited INTEGER NOT NULL DEFAULT 0, list_name TEXT, note_title TEXT, title TEXT, explicit INTEGER NOT NULL DEFAULT 0);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    subject TEXT,
    fact TEXT NOT NULL,
    source_at REAL
, status TEXT NOT NULL DEFAULT 'current', superseded_by INTEGER, retired_at REAL, retired_reason TEXT, erased_at REAL, kind TEXT);

CREATE TABLE conversation_people (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (conversation_id, name)
);

CREATE TABLE digests (
    date TEXT PRIMARY KEY,                    -- YYYY-MM-DD (UTC)
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE speakers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_owner INTEGER NOT NULL DEFAULT 0,
    embedding BLOB NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    speaker_id INTEGER NULL REFERENCES speakers(id) ON DELETE SET NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    merged_into INTEGER NULL REFERENCES people(id)
);

CREATE UNIQUE INDEX people_name ON people(name) WHERE merged_into IS NULL;

CREATE TABLE people_aliases (
    person_id INTEGER NOT NULL REFERENCES people(id),
    alias TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (person_id, alias)
);

CREATE INDEX people_aliases_alias ON people_aliases(alias);

CREATE TABLE embeddings (
    kind TEXT NOT NULL,                       -- conversation|fact|action_item
    ref_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

CREATE TABLE voice_samples (
    id INTEGER PRIMARY KEY,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    source TEXT NOT NULL,                     -- enrolled|named|legacy
    created_at REAL NOT NULL
);

CREATE INDEX voice_samples_speaker ON voice_samples(speaker_id);

CREATE TABLE fact_sources (
    fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_at REAL,
    PRIMARY KEY (fact_id, conversation_id)
);

CREATE TABLE module_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE speaker_candidates (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (session_id, label)
);

CREATE TABLE forget_rewrites (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    facts TEXT NOT NULL DEFAULT '[]',         -- JSON: the erased facts' sentences
    title TEXT,
    summary TEXT,
    key_points TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    queued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE forget_digests (
    date TEXT PRIMARY KEY,
    conversation_ids TEXT NOT NULL DEFAULT '[]',
    queued_at REAL NOT NULL
);

CREATE TABLE erased_sources (
    fact_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    erased_at REAL NOT NULL,
    PRIMARY KEY (fact_id, conversation_id)
);

CREATE TABLE written_notes (
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

CREATE INDEX written_notes_conv ON written_notes(conversation_id);

CREATE TABLE erased_facts (
    id INTEGER PRIMARY KEY,                   -- the fact's id
    fact TEXT NOT NULL,
    erased_at REAL NOT NULL
);

CREATE VIRTUAL TABLE segments_fts USING fts5(
    text, content='segments', content_rowid='id', tokenize = 'porter unicode61'
);

CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER segments_au AFTER UPDATE OF text ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE search_fts USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, conversation_id UNINDEXED, body,
    tokenize = 'porter unicode61'
);
