"""The SQLite connection: busy timeout, write transactions, the set-up lock."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic
from typing import Any

log = logging.getLogger(__name__)


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


# How long a connection waits for another connection's lock before "database is locked"
# (what Python's sqlite3 module sets by default, now explicit).
BUSY_TIMEOUT_MS = 5000
# A write that waited longer than this for the lock is logged.
LOCK_WAIT_WARN_SECONDS = 1.0


def _begin_immediate(c: sqlite3.Connection) -> None:
    """Start a write transaction, logging a long wait for the write lock."""
    start = monotonic()
    c.execute("BEGIN IMMEDIATE")
    waited = monotonic() - start
    if waited > LOCK_WAIT_WARN_SECONDS:
        log.warning("waited %.1f s for the database write lock", waited)
