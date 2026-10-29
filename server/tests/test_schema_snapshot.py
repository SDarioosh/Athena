"""An old database upgraded by today's ``Store`` ends up with a fresh database's schema.

Each fixture in ``tests/fixtures/`` is the schema of a database an older version of
``store.py`` created (its header names the source commit). The test loads it with that
version's ``user_version``, adds a few fictional rows, opens it with today's ``Store``
(which runs the migrations) and compares the result with a database today's ``Store``
creates from nothing: tables and their columns (``PRAGMA table_info``, each column's
definition, foreign keys, table constraints), indexes, triggers and virtual tables.

Known differences are listed in ``EXPECTED_DIFFERENCES``. The actual differences must
equal that list exactly: a new drift fails, and so does an entry that no longer occurs,
so fixing a drift means deleting its entry.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from convo_server.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

# fixture file -> the user_version a database with that schema has
OLD_SCHEMAS = {
    # the first store.py never set user_version; its first migration ("< 2") jumped to 2
    "schema_v1.sql": 0,
    "schema_v14.sql": 14,
}


class Diff(NamedTuple):
    fixture: str
    obj: str      # table, index, trigger or virtual table
    aspect: str   # what about it differs
    detail: str   # migrated vs fresh


# Differences between a migrated old database and a fresh one, as they are today.
# Each one: (fixture, object, aspect, detail) and why. Remove an entry when the drift is
# fixed; the test fails while an entry is listed that no longer occurs.
# None today: a fresh database runs the same MIGRATIONS steps as an old one, from the
# frozen first schema, so both end up identical.
EXPECTED_DIFFERENCES: list[tuple[Diff, str]] = []


# ---------------------------------------------------------------- schema description

def _norm(sql: str | None) -> str:
    """SQL text without comments, double quotes and cosmetic whitespace."""
    if not sql:
        return ""
    sql = re.sub(r"--[^\n]*", "", sql).replace('"', "")
    sql = re.sub(r"\s+", " ", sql)
    sql = re.sub(r"\s*([(),])\s*", r"\1", sql)
    return sql.strip()


def _split_top(body: str) -> list[str]:
    """Split a CREATE TABLE body at the commas that are not inside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


_CONSTRAINT = re.compile(r"^(PRIMARY KEY|UNIQUE|CHECK|FOREIGN KEY|CONSTRAINT)\b", re.I)


def _table_defs(sql: str) -> tuple[dict[str, str], list[str]]:
    """Each column's definition (collation, CHECK, REFERENCES included) by name, and the
    table constraints, from a CREATE TABLE statement."""
    s = _norm(sql)
    body, tail = s[s.index("(") + 1:s.rindex(")")], s[s.rindex(")") + 1:].strip()
    cols: dict[str, str] = {}
    constraints = [tail] if tail else []
    for part in _split_top(body):
        if _CONSTRAINT.match(part):
            constraints.append(part)
        else:
            name, _, decl = part.partition(" ")
            cols[name] = decl
    return cols, sorted(constraints)


def describe(path: Path) -> dict[str, dict[str, Any]]:
    """The schema of the database at ``path``, keyed by object name."""
    c = sqlite3.connect(path)
    try:
        out: dict[str, dict[str, Any]] = {}
        for typ, name, tbl, sql in c.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"):
            if typ == "table" and _norm(sql).upper().startswith("CREATE VIRTUAL TABLE"):
                out[name] = {"type": "virtual table", "sql": _norm(sql)}
            elif typ == "table":
                info = [tuple(r[1:]) for r in c.execute(f"PRAGMA table_info('{name}')")]
                cols, constraints = _table_defs(sql)
                out[name] = {
                    "type": "table",
                    "order": [r[0] for r in info],
                    "columns": {r[0]: {"type": r[1], "notnull": r[2], "default": r[3],
                                       "pk": r[4], "definition": cols.get(r[0])}
                                for r in info},
                    "foreign keys": sorted(tuple(r[2:]) for r in c.execute(
                        f"PRAGMA foreign_key_list('{name}')")),
                    "constraints": constraints,
                }
            elif typ == "index":
                listed = {r[1]: r for r in c.execute(f"PRAGMA index_list('{tbl}')")}
                _, _, unique, origin, partial = listed[name]
                keys = [(r[2], r[4], r[5]) for r in c.execute(f"PRAGMA index_xinfo('{name}')")
                        if r[5]]  # (column, desc, collation) of the key columns
                out[name] = {"type": "index", "table": tbl, "unique": unique,
                             "origin": origin, "partial": partial, "keys": keys,
                             "sql": _norm(sql)}
            else:  # trigger, view
                out[name] = {"type": typ, "table": tbl, "sql": _norm(sql)}
        return out
    finally:
        c.close()


def schema_diffs(fixture: str, migrated: dict, fresh: dict) -> set[Diff]:
    diffs: set[Diff] = set()
    for name in sorted(set(migrated) | set(fresh)):
        a, b = migrated.get(name), fresh.get(name)
        if a is None or b is None:
            here = "fresh" if a is None else "migrated"
            diffs.add(Diff(fixture, name, "presence", f"{(a or b)['type']} only in {here}"))
            continue
        if a["type"] != b["type"]:
            diffs.add(Diff(fixture, name, "type", f"migrated {a['type']} | fresh {b['type']}"))
            continue
        if a["type"] != "table":
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    diffs.add(Diff(fixture, name, key,
                                   f"migrated {a.get(key)!r} | fresh {b.get(key)!r}"))
            continue
        ca, cb = a["columns"], b["columns"]
        for col in sorted(set(ca) | set(cb)):
            if col not in ca or col not in cb:
                here = "fresh" if col not in ca else "migrated"
                diffs.add(Diff(fixture, name, f"column {col}", f"only in {here}"))
            elif ca[col] != cb[col]:
                diffs.add(Diff(fixture, name, f"column {col}",
                               f"migrated {ca[col]!r} | fresh {cb[col]!r}"))
        common_a = [c for c in a["order"] if c in cb]
        common_b = [c for c in b["order"] if c in ca]
        if common_a != common_b:
            i = next(i for i, (x, y) in enumerate(zip(common_a, common_b, strict=False)) if x != y)
            diffs.add(Diff(fixture, name, "column order",
                           f"migrated {','.join(common_a[i - 1:])} | "
                           f"fresh {','.join(common_b[i - 1:])}"))
        for key in ("foreign keys", "constraints"):
            if a[key] != b[key]:
                diffs.add(Diff(fixture, name, key, f"migrated {a[key]!r} | fresh {b[key]!r}"))
    return diffs


# ------------------------------------------------------------------- old databases

# Fictional rows, using only columns the first schema had (every later one has them).
OLD_ROWS = """
INSERT INTO sessions (id, device, started_at, last_chunk_at, last_speech_at, received_at,
                      status)
VALUES ('sess-1', 'omi', 1760000000, 1760000060, 1760000055, 1760000061, 'done');
INSERT INTO chunks (session_id, sequence, started_at, duration, codec, audio_path, speech,
                    received_at)
VALUES ('sess-1', 0, 1760000000, 30, 'opus', NULL, '[[0.5, 29.0]]', 1760000031),
       ('sess-1', 1, 1760000030, 30, 'opus', NULL, '[[0.0, 25.0]]', 1760000061);
INSERT INTO conversations (id, device, started_at, ended_at, status, title, summary,
                           key_points, topics, people, updated_at)
VALUES ('conv-1', 'omi', 1760000000, 1760000060, 'summarized', 'Bakery plans',
        'Talked about the bakery.', '["Open on Monday"]', '["bakery"]',
        '["Marisol''s"]', 1760000100);
INSERT INTO segments (id, session_id, conversation_id, device, started_at, ended_at,
                      speaker, is_owner, text)
VALUES (1, 'sess-1', 'conv-1', 'omi', 1760000001, 1760000005, 'Me', 1,
        'The bakery opens on Monday.'),
       (2, 'sess-1', 'conv-1', 'omi', 1760000006, 1760000010, 'Speaker 1', 0,
        'I will bring the sourdough starter.');
INSERT INTO action_items (id, conversation_id, text, owner, due, status, source_at)
VALUES (1, 'conv-1', 'Bring the sourdough starter to the bakery', 'Speaker 1', 'Monday',
        'open', 1760000006);
INSERT INTO facts (id, conversation_id, subject, fact, source_at)
VALUES (1, 'conv-1', 'owner', 'Is opening a bakery on Monday.', 1760000001);
INSERT INTO conversation_people (conversation_id, name) VALUES ('conv-1', 'Marisol''s');
INSERT INTO digests (date, content, created_at)
VALUES ('2025-10-09', 'One conversation about the bakery.', 1760000200);
INSERT INTO speakers (id, name, is_owner, embedding, created_at)
VALUES (1, 'Me', 1, x'0000803f00000000', 1759990000);
INSERT INTO embeddings (kind, ref_id, conversation_id, model, vector)
VALUES ('fact', '1', 'conv-1', 'fake-embedder', x'0000803f');
INSERT INTO search_fts (kind, ref_id, conversation_id, body)
VALUES ('fact', '1', 'conv-1', 'Is opening a bakery on Monday.');
"""

OLD_COUNTS = {"sessions": 1, "chunks": 2, "conversations": 1, "segments": 2,
              "action_items": 1, "facts": 1, "digests": 1, "speakers": 1, "embeddings": 1}


def old_database(path: Path, fixture: str) -> Path:
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / fixture).read_text())
    c.execute(f"PRAGMA user_version = {OLD_SCHEMAS[fixture]}")
    c.executescript(OLD_ROWS)
    c.close()
    return path


def user_version(path: Path) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    path = tmp_path_factory.mktemp("fresh") / "convo.db"
    Store(path).close()
    return path, describe(path)


@pytest.fixture(scope="module", params=sorted(OLD_SCHEMAS))
def migrated(request: pytest.FixtureRequest,
             tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path, dict]:
    fixture = request.param
    path = old_database(tmp_path_factory.mktemp(fixture.removesuffix(".sql")) / "convo.db",
                        fixture)
    Store(path).close()
    return fixture, path, describe(path)


# --------------------------------------------------------------------------- tests

def _fmt(diffs: set[Diff]) -> str:
    return "\n".join(f"  {d.obj} / {d.aspect}: {d.detail}" for d in sorted(diffs)) or "  -"


def test_migrated_schema_matches_fresh_except_expected(migrated, fresh) -> None:
    fixture, _, schema = migrated
    actual = schema_diffs(fixture, schema, fresh[1])
    expected = {d for d, _why in EXPECTED_DIFFERENCES if d.fixture == fixture}
    assert actual == expected, (
        f"{fixture}: schema drift between a migrated and a fresh database changed.\n"
        f"New differences (fix the migration, or list them in EXPECTED_DIFFERENCES):\n"
        f"{_fmt(actual - expected)}\n"
        f"Listed but gone (delete them from EXPECTED_DIFFERENCES):\n"
        f"{_fmt(expected - actual)}")


def test_expected_differences_are_unique() -> None:
    keys = [d for d, _why in EXPECTED_DIFFERENCES]
    assert len(keys) == len(set(keys))
    assert {d.fixture for d in keys} <= set(OLD_SCHEMAS)


def test_user_version_matches_fresh(migrated, fresh) -> None:
    _, path, _ = migrated
    assert user_version(path) == user_version(fresh[0]) > max(OLD_SCHEMAS.values())


def test_old_rows_survive_migration(migrated) -> None:
    fixture, path, _ = migrated
    version = OLD_SCHEMAS[fixture]
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        for table, n in OLD_COUNTS.items():
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n, table
        # chunks were rebuilt with a track (step 15): same rows, no track
        chunks = [dict(r) for r in c.execute(
            "SELECT sequence, speech, track, track_name FROM chunks ORDER BY sequence")]
        assert chunks == [
            {"sequence": 0, "speech": "[[0.5, 29.0]]", "track": "", "track_name": None},
            {"sequence": 1, "speech": "[[0.0, 25.0]]", "track": "", "track_name": None}]
        seg = [tuple(r) for r in c.execute(
            "SELECT id, speaker, text, kind, erased FROM segments ORDER BY id")]
        assert seg == [(1, "Me", "The bakery opens on Monday.", "speech", 0),
                       (2, "Speaker 1", "I will bring the sourdough starter.", "speech", 0)]
        # the line index was rebuilt (step 2) or kept in sync by the triggers
        assert [r[0] for r in c.execute(
            "SELECT rowid FROM segments_fts WHERE segments_fts MATCH 'sourdough'")] == [2]
        assert c.execute("SELECT kind FROM sessions").fetchone()[0] == "conversation"
        item = dict(c.execute("SELECT * FROM action_items").fetchone())
        assert item["text"] == "Bring the sourdough starter to the bakery"
        assert item["kind"] == "task" and item["status"] == "open"
        if version < 6:  # the conversation's start, when the row had no time
            assert item["created_at"] == 1760000000
        if version < 11:  # a title from the start of the text
            assert item["title"] == "Bring the sourdough starter to the bakery"
        fact = dict(c.execute("SELECT * FROM facts").fetchone())
        assert fact["fact"] == "Is opening a bakery on Monday." and fact["status"] == "current"
        conv = dict(c.execute("SELECT * FROM conversations").fetchone())
        assert conv["title"] == "Bakery plans" and conv["summary"] == "Talked about the bakery."
        if version < 12:  # the possessive is dropped from people's names
            assert conv["people"] == '["Marisol"]'
            assert [r[0] for r in c.execute("SELECT name FROM conversation_people")] == [
                "Marisol"]
        assert c.execute("SELECT embedding FROM speakers").fetchone()[0] == bytes.fromhex(
            "0000803f00000000")
    finally:
        c.close()


def test_backfilled_rows_on_first_schema(tmp_path: Path) -> None:
    path = old_database(tmp_path / "convo.db", "schema_v1.sql")
    Store(path).close()
    c = sqlite3.connect(path)
    try:
        # step 3: the blended voiceprint became the speaker's first voice sample
        assert c.execute("SELECT speaker_id, source FROM voice_samples").fetchall() == [
            (1, "legacy")]
        # step 10: one person per speaker and per name mentioned
        assert sorted(c.execute(
            "SELECT name, speaker_id, is_owner FROM people WHERE merged_into IS NULL")) == [
            ("Marisol", None, 0), ("Me", 1, 1)]
    finally:
        c.close()


def test_migrating_again_changes_nothing(migrated) -> None:
    fixture, path, schema = migrated
    version = user_version(path)
    c = sqlite3.connect(path)
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in (*OLD_COUNTS, "people", "voice_samples", "conversation_people")}
    c.close()
    Store(path).close()
    assert describe(path) == schema
    assert user_version(path) == version
    c = sqlite3.connect(path)
    try:
        for table, n in counts.items():
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n, table
    finally:
        c.close()
