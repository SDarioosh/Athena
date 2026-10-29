#!/usr/bin/env python3
"""Try the database upgrade on a COPY of a database and report what it changed.

Make the copy with SQLite's online backup (safe while the services run), then run this
on the copy; it never opens any other file:

    sqlite3 server/data/convo.db ".backup /tmp/convo-check.db"
    cd server && .venv/bin/python scripts/check_migration.py /tmp/convo-check.db

It prints user_version, the tables and each table's row count before and after opening
the copy with today's ``Store`` (which runs the migrations), the columns a new database
has that the upgraded copy lacks (none expected), and ``PRAGMA integrity_check``. Exit
status 0 when the copy reached the latest version, lost no rows, misses no column and is
intact; 1 otherwise. Delete the copy afterwards (it holds real data).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from convo_server.store import SCHEMA_VERSION, Store


def snapshot(path: Path) -> tuple[int, dict[str, int]]:
    """user_version and row count per table (reads only)."""
    c = sqlite3.connect(path)
    try:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return version, {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                         for t in tables}
    finally:
        c.close()


def columns(c: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: {r[1] for r in c.execute(f"PRAGMA table_info('{t}')")} for t in tables}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("copy", type=Path, help="a copy of the database (it gets upgraded)")
    args = ap.parse_args(argv)
    path: Path = args.copy
    if not path.is_file():
        print(f"no such file: {path}")
        return 1
    if path.name == "convo.db" or any(
            path.with_name(path.name + s).exists() for s in ("-wal", "-shm")):
        print(f"{path} looks like a live database (named convo.db, or a -wal/-shm file "
              "next to it). Run this on a copy made with sqlite3 .backup.")
        return 1

    v_before, before = snapshot(path)
    Store(path).close()
    v_after, after = snapshot(path)

    print(f"user_version: {v_before} -> {v_after} (latest {SCHEMA_VERSION})")
    print(f"{'table':<24}{'before':>10}{'after':>10}")
    problems = []
    for t in sorted(set(before) | set(after)):
        b, a = before.get(t), after.get(t)
        mark = ""
        if a is None:
            mark = "  <- table gone"
            problems.append(f"table {t} gone")
        elif b is None:
            mark = "  (new table)"
        elif a != b:
            mark = "  <- row count changed"
            problems.append(f"{t}: {b} -> {a} rows")
        print(f"{t:<24}{'-' if b is None else b:>10}{'-' if a is None else a:>10}{mark}")

    fresh = Store(":memory:")
    try:
        want = columns(fresh._conn)
    finally:
        fresh.close()
    c = sqlite3.connect(path)
    try:
        have = columns(c)
        integrity = [r[0] for r in c.execute("PRAGMA integrity_check")]
    finally:
        c.close()
    missing = sorted(f"{t}.{col}" for t, cols in want.items()
                     for col in cols - have.get(t, set()))
    print(f"columns a new database has that the copy lacks: {', '.join(missing) or 'none'}")
    print(f"integrity_check: {'; '.join(integrity)}")

    if v_after != SCHEMA_VERSION:
        problems.append(f"user_version {v_after}, expected {SCHEMA_VERSION}")
    if missing:
        problems.append("missing columns")
    if integrity != ["ok"]:
        problems.append("integrity_check failed")
    print("RESULT: " + ("OK" if not problems else "PROBLEM: " + "; ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
