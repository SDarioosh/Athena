"""``Store``: the one object callers use; it opens the database and has every area's
methods."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .action_items import ActionItemsRepo
from .connection import BUSY_TIMEOUT_MS, _begin_immediate, _init_lock, _row
from .conversations import ConversationsRepo
from .facts import FactsRepo
from .forget import ForgetRepo
from .meetings import MeetingsRepo
from .migrations import migrate
from .people import PeopleRepo
from .search import SearchRepo
from .sessions import SessionsRepo
from .voices import VoicesRepo


class Store(SessionsRepo, MeetingsRepo, ConversationsRepo, FactsRepo, ForgetRepo, SearchRepo,
            PeopleRepo, VoicesRepo, ActionItemsRepo):
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                     timeout=BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # deleted rows are overwritten, not left in free pages: a forgotten fact or a
        # pruned transcript must not stay readable in the file
        self._conn.execute("PRAGMA secure_delete = ON")
        self._conn.execute("PRAGMA foreign_keys = ON")
        # The API server and the MCP server open the same database at the same time
        # (deploy starts both): set up and migrate one process at a time.
        with _init_lock(self.path):
            self._conn.execute("PRAGMA journal_mode = WAL")
            migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            _begin_immediate(self._conn)
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
