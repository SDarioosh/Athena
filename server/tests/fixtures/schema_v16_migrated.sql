-- An Athena database at user_version 16 that was upgraded, not created at 16, for
-- test_migrations.py: the shape of a long-running install.
--
-- Source: schema_v14.sql loaded with user_version 14 and opened with the Store of
-- server/convo_server/store.py at commit b96332dce8428fa39c4cbd067b56da621b9568c0 (main on
-- 2026-10-29, "Merge pull request #152"), the last store.py before the MIGRATIONS registry
-- (step 17), which ran its steps 15 and 16 and its unnumbered changes; then dumped like
-- schema_v14.sql (sqlite_master in creation order, without sqlite_autoindex_* and the
-- FTS5 shadow tables). Load it with user_version 16. Fictional schema only, no data.

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
, erased INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'speech');

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
, session_id TEXT);

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

CREATE TABLE voice_enrollment (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE meeting_info (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    topic TEXT,
    participants TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);

CREATE INDEX meeting_info_conv ON meeting_info(conversation_id);

CREATE TABLE meeting_chat (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    line_key TEXT NOT NULL,
    PRIMARY KEY (session_id, line_key)
);

CREATE TABLE meeting_live (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    track TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, track)
);

CREATE INDEX sessions_status ON sessions(status);

CREATE TABLE "chunks" ( session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, started_at REAL NOT NULL, duration REAL NOT NULL, codec TEXT NOT NULL, audio_path TEXT, speech TEXT NOT NULL DEFAULT '[]', received_at REAL NOT NULL, track TEXT NOT NULL DEFAULT '', track_name TEXT, PRIMARY KEY (session_id, track, sequence));

CREATE TABLE meetings (
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

CREATE INDEX meetings_token ON meetings(upload_token_hash);
