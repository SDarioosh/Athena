"""The MIGRATIONS registry: each step in its own transaction, the upgrade of a version 16
database (today's installs) to 17, and opening twice changing nothing.

``test_schema_snapshot.py`` compares old databases with a fresh one; this file adds the
version 16 fixtures (one created new at 16, one upgraded to 16 the way a long-running
install was), a failure in the middle of a step, and the store's connection settings.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_schema_snapshot import FIXTURES, OLD_ROWS, describe, schema_diffs, user_version

import athena.db.connection as connection_mod
import athena.db.migrations as migrations_mod
from athena.db.migrations import MIGRATIONS, SCHEMA_VERSION
from athena.db.store import Store

V16_FIXTURES = ("schema_v16.sql", "schema_v16_migrated.sql")

# Fictional rows in tables and columns a version 16 database has, besides OLD_ROWS.
V16_ROWS = """
INSERT INTO sessions (id, device, started_at, last_chunk_at, last_speech_at, received_at,
                      status, kind)
VALUES ('meet-1', 'zoom', 1760100000, 1760100030, 1760100025, 1760100031, 'done',
        'conversation');
INSERT INTO chunks (session_id, sequence, started_at, duration, codec, audio_path, speech,
                    received_at, track, track_name)
VALUES ('meet-1', 0, 1760100000, 30, 'opus', NULL, '[[1.0, 20.0]]', 1760100031, 'u-7',
        'Priya');
INSERT INTO conversations (id, device, started_at, ended_at, status, title, summary,
                           updated_at, kind)
VALUES ('conv-m', 'zoom', 1760100000, 1760100030, 'summarized', 'Kiln schedule',
        'Planned the kiln firing.', 1760100100, 'conversation');
INSERT INTO segments (id, session_id, conversation_id, device, started_at, ended_at,
                      speaker, is_owner, text, erased, kind)
VALUES (10, 'meet-1', 'conv-m', 'zoom', 1760100002, 1760100006, 'Priya', 0,
        'The kiln fires on Thursday.', 0, 'speech'),
       (11, 'meet-1', 'conv-m', 'zoom', 1760100008, 1760100008, 'Priya', 0,
        'link to the glaze sheet', 0, 'chat');
INSERT INTO meeting_info (session_id, conversation_id, topic, participants, updated_at)
VALUES ('meet-1', 'conv-m', 'Pottery club', '[{"name": "Priya"}]', 1760100100);
INSERT INTO meeting_chat (session_id, line_key) VALUES ('meet-1', 'abc123');
INSERT INTO meetings (id, state, session_id, meeting_id, title, runner, created_at)
VALUES ('mtg-1', 'ended', 'meet-1', '81234567890', 'Pottery club', 'plain', 1760099990);
INSERT INTO voice_samples (speaker_id, embedding, source, created_at, session_id)
VALUES (1, x'0000803f00000000', 'named', 1760100200, 'meet-1');
INSERT INTO people (id, name, speaker_id, is_owner, created_at)
VALUES (1, 'Me', 1, 1, 1759990000), (2, 'Priya', NULL, 0, 1760100000);
INSERT INTO people_aliases (person_id, alias) VALUES (2, 'Pri');
INSERT INTO module_settings (key, value, updated_at)
VALUES ('vocabulary', '"kiln, glaze"', 1760100300);
INSERT INTO written_notes (source_id, session_id, conversation_id, link, written_at,
                           text_hash, created_at, updated_at)
VALUES ('hub-note:5', 'written:hub-note:5', 'conv-1', 'http://hub.example/notes/5',
        1760000500, 'f00d', 1760000500, 1760000500);
"""


def _connect(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _tables(path: Path) -> list[str]:
    c = _connect(path)
    try:
        return [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%fts_%' ORDER BY name")]
    finally:
        c.close()


def _contents(path: Path) -> dict[str, list[tuple]]:
    """Every row of every ordinary table, sorted."""
    c = _connect(path)
    try:
        return {t: sorted((tuple(r) for r in c.execute(f"SELECT * FROM {t}")), key=repr)
                for t in _tables(path)}
    finally:
        c.close()


def v16_database(path: Path, fixture: str) -> Path:
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / fixture).read_text())
    c.execute("PRAGMA user_version = 16")
    c.executescript(OLD_ROWS + V16_ROWS)
    c.close()
    return path


def v14_database(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / "schema_v14.sql").read_text())
    c.execute("PRAGMA user_version = 14")
    c.executescript(OLD_ROWS)
    c.close()
    return path


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> dict:
    path = tmp_path_factory.mktemp("fresh") / "convo.db"
    Store(path).close()
    return describe(path)


# ------------------------------------------------------------------ the registry

def test_registry_is_numbered_by_version() -> None:
    assert MIGRATIONS[0] is None and all(MIGRATIONS[1:])
    assert SCHEMA_VERSION == len(MIGRATIONS) - 1 == 17
    for version, step in enumerate(MIGRATIONS[1:], start=1):
        assert step.__name__.startswith(f"_m{version:02d}_")


def test_new_database_is_at_the_last_version(tmp_path: Path) -> None:
    Store(tmp_path / "convo.db").close()
    assert user_version(tmp_path / "convo.db") == SCHEMA_VERSION


def test_first_schema_has_only_first_version_tables() -> None:
    c = sqlite3.connect(":memory:")
    c.executescript(migrations_mod.SCHEMA)
    v1 = sqlite3.connect(":memory:")
    v1.executescript((FIXTURES / "schema_v1.sql").read_text())
    names = "SELECT type, name FROM sqlite_master ORDER BY name"
    assert c.execute(names).fetchall() == v1.execute(names).fetchall()


# ------------------------------------------------------------- version 16 -> 17

@pytest.mark.parametrize("fixture", V16_FIXTURES)
def test_v16_database_migrates_with_data_intact(tmp_path: Path, fresh: dict,
                                                fixture: str) -> None:
    path = v16_database(tmp_path / "convo.db", fixture)
    before = _contents(path)
    Store(path).close()
    assert user_version(path) == 17
    after = _contents(path)
    # same rows in every table that was there (nothing rewritten); nothing new is filled
    assert {t: rows for t, rows in after.items() if t in before} == before
    assert all(not rows for t, rows in after.items() if t not in before)
    c = _connect(path)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [r[0] for r in c.execute(
            "SELECT rowid FROM segments_fts WHERE segments_fts MATCH 'kiln'")] == [10]
        assert c.execute("SELECT kind FROM segments WHERE id = 11").fetchone()[0] == "chat"
    finally:
        c.close()
    diffs = schema_diffs(fixture, describe(path), fresh)
    if fixture == "schema_v16_migrated.sql":
        assert diffs == set()
    else:
        # created at 16 by a SCHEMA that declared segments.kind inline: kind sits before
        # erased. Step 17 doesn't rebuild segments for that (code reads by name).
        assert {(d.obj, d.aspect) for d in diffs} == {("segments", "column order")}


def test_v16_database_without_later_tables_gets_them(tmp_path: Path, fresh: dict) -> None:
    """A database last opened before a table was added (voice_enrollment, 2026-10-28),
    or before voice_samples.session_id: step 17 adds them."""
    path = tmp_path / "convo.db"
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / "schema_v16_migrated.sql").read_text())
    c.executescript("DROP TABLE voice_enrollment; DROP TABLE meeting_live;"
                    " DROP INDEX sessions_status;"
                    " ALTER TABLE voice_samples DROP COLUMN session_id;")
    c.execute("PRAGMA user_version = 16")
    c.executescript(OLD_ROWS)
    c.close()
    Store(path).close()
    assert schema_diffs("v16", describe(path), fresh) == set()
    store = Store(path)
    store.put_voice_enrollment({"state": "waiting"})
    assert store.get_voice_enrollment() == {"state": "waiting"}
    store.close()


@pytest.mark.parametrize("fixture", V16_FIXTURES)
def test_opening_twice_changes_nothing(tmp_path: Path, fixture: str) -> None:
    path = v16_database(tmp_path / "convo.db", fixture)
    Store(path).close()
    schema, contents = describe(path), _contents(path)
    Store(path).close()
    assert describe(path) == schema and _contents(path) == contents
    assert user_version(path) == 17


def test_opening_a_new_database_twice_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "convo.db"
    Store(path).close()
    schema, contents = describe(path), _contents(path)
    Store(path).close()
    assert describe(path) == schema and _contents(path) == contents


# ------------------------------------------------------------- failure mid-step

def _fail_after(monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    """Make step ``version`` do all its work and then raise."""
    steps = list(migrations_mod.MIGRATIONS)
    steps[version] = _failing_after(steps[version])
    monkeypatch.setattr(migrations_mod, "MIGRATIONS", steps)


def _failing_after(step):
    def fail(c: sqlite3.Connection) -> None:
        step(c)
        raise RuntimeError("power cut")
    fail.__name__ = step.__name__
    return fail


def test_failed_step_leaves_previous_version_without_its_changes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = v14_database(tmp_path / "convo.db")
    before = _contents(path)
    # step 15 rebuilds chunks; step 16 runs it all, then fails
    _fail_after(monkeypatch, 16)
    with pytest.raises(RuntimeError, match="power cut"):
        Store(path)
    assert user_version(path) == 15  # step 15 committed, 16 rolled back
    assert "meetings" not in _tables(path)
    c = _connect(path)
    cols = [r[1] for r in c.execute("PRAGMA table_info(chunks)")]
    c.close()
    assert cols[-2:] == ["track", "track_name"]

    _fail_after(monkeypatch, 15)
    path2 = v14_database(tmp_path / "convo2.db")
    with pytest.raises(RuntimeError, match="power cut"):
        Store(path2)
    assert user_version(path2) == 14
    c = _connect(path2)
    try:
        assert "track" not in {r[1] for r in c.execute("PRAGMA table_info(chunks)")}
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'chunks_v15'"
                         ).fetchone() is None
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()
    assert _contents(path2) == before

    monkeypatch.undo()  # the next start completes both
    for p in (path, path2):
        Store(p).close()
        assert user_version(p) == 17


def test_failed_last_step_on_v16(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "convo.db"
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / "schema_v16_migrated.sql").read_text())
    c.executescript("DROP TABLE voice_enrollment;")
    c.execute("PRAGMA user_version = 16")
    c.close()
    schema = describe(path)
    _fail_after(monkeypatch, 17)
    with pytest.raises(RuntimeError):
        Store(path)
    assert user_version(path) == 16 and describe(path) == schema


# ------------------------------------------------------------ connection settings

def test_busy_timeout_is_set(tmp_path: Path) -> None:
    store = Store(tmp_path / "convo.db")
    try:
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        store.close()


def test_long_lock_wait_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_mod, "LOCK_WAIT_WARN_SECONDS", 0.2)
    store = Store(tmp_path / "convo.db")
    other = sqlite3.connect(tmp_path / "convo.db", isolation_level=None,
                            check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(0.5, lambda: other.execute("COMMIT")).start()
    try:
        with caplog.at_level(logging.WARNING, logger="athena.db.connection"):
            store.set_setting("vocabulary", "kiln")
        assert any("write lock" in r.message for r in caplog.records)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="athena.db.connection"):
            store.set_setting("vocabulary", "glaze")
        assert not caplog.records
    finally:
        time.sleep(0.6)
        other.close()
        store.close()


def test_crash_inside_a_data_step_rewrites_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 12 rewrites names in two tables, then renames the person; a crash between
    them used to leave the names rewritten at version 11."""
    path = tmp_path / "convo.db"
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / "schema_v1.sql").read_text())
    c.executescript(OLD_ROWS)
    c.close()

    def crash(*_args: object) -> None:
        raise RuntimeError("crash in step 12")

    monkeypatch.setattr(migrations_mod, "_rename_person", crash)
    with pytest.raises(RuntimeError):
        Store(path)
    assert user_version(path) == 11
    c = sqlite3.connect(path)
    try:
        assert c.execute("SELECT name FROM conversation_people").fetchall() == [
            ("Marisol's",)]
        assert c.execute("SELECT people FROM conversations").fetchone()[0] == '["Marisol\'s"]'
    finally:
        c.close()
    monkeypatch.undo()
    Store(path).close()
    c = sqlite3.connect(path)
    try:
        assert c.execute("SELECT name FROM conversation_people").fetchall() == [("Marisol",)]
        assert c.execute("SELECT name FROM people WHERE merged_into IS NULL ORDER BY name"
                         ).fetchall() == [("Marisol",), ("Me",)]
    finally:
        c.close()


# ------------------------------------------------------------ scripts/check_migration.py

def _check_migration():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_migration", Path(__file__).parents[1] / "scripts" / "check_migration.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_migration_script(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    check = _check_migration()
    (tmp_path / "live").mkdir()
    live = v16_database(tmp_path / "live" / "convo.db", "schema_v16_migrated.sql")
    c = sqlite3.connect(live)
    c.execute("DROP TABLE voice_enrollment")
    c.execute("PRAGMA journal_mode = WAL")  # like the real database
    c.close()
    # what the owner does: sqlite3 convo.db ".backup /tmp/convo-check.db"
    path = tmp_path / "convo-check.db"
    src, dst = sqlite3.connect(live), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    original = live.read_bytes()
    assert check.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "user_version: 16 -> 17 (latest 17)" in out
    assert "voice_enrollment" in out and "(new table)" in out
    assert "segments                         4         4" in out
    assert "lacks: none" in out and "integrity_check: ok" in out
    assert out.rstrip().endswith("RESULT: OK")
    assert live.read_bytes() == original and user_version(live) == 16
    # running it again finds nothing to do
    assert check.main([str(path)]) == 0
    assert "user_version: 17 -> 17" in capsys.readouterr().out


def test_check_migration_refuses_a_live_database(tmp_path: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    check = _check_migration()
    live = tmp_path / "convo.db"
    Store(live).close()
    assert check.main([str(live)]) == 1
    assert "looks like a live database" in capsys.readouterr().out
