"""Anchors for chunks that still carry TIME_UNSET when they reach the relay (§2.3).

Such a chunk's `start_ms` is milliseconds since its session started. It's uploaded with
`timestamp = anchor + start_ms`, where `anchor` is the relay's clock when it first saw the
session. Anchors are kept in a small JSON file so every chunk of the session gets the same
anchor across connections and restarts. Best effort: if the file can't be read or written,
the anchors live in memory only and a warning is logged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("athena_relay.anchors")

KEEP_SECONDS = 60 * 24 * 3600  # forget anchors of sessions not seen for 60 days


def default_state_dir() -> Path:
    """`<state base>/athena-relay`; a relay set up before the rename keeps using its existing
    `barvis-relay` dir as is (never moved or copied), so acknowledged chunks' anchors and the
    PDA's feed hash carry on."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    new, old = base / "athena-relay", base / "barvis-relay"
    if not new.exists() and old.is_dir():
        return old
    return new


class Anchors:
    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time,
                 persist: bool = True) -> None:
        self.path = path
        self.clock = clock
        self.persist = persist and path is not None
        self._data: dict[str, dict[str, int]] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("can't read anchors from %s (%s); starting empty", self.path, e)
            return
        if isinstance(raw, dict):
            for sid, v in raw.items():
                if isinstance(v, dict) and isinstance(v.get("anchor_ms"), int):
                    self._data[sid] = {"anchor_ms": v["anchor_ms"],
                                       "seen_ms": int(v.get("seen_ms", v["anchor_ms"]))}

    def _save(self) -> None:
        if not self.persist:
            return
        assert self.path is not None
        now_ms = int(self.clock() * 1000)
        self._data = {k: v for k, v in self._data.items()
                      if now_ms - v["seen_ms"] < KEEP_SECONDS * 1000}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("can't save anchors to %s (%s); keeping them in memory", self.path, e)

    def anchor_ms(self, session_hex: str) -> int:
        """The anchor for a session, created (relay clock now) the first time it's seen."""
        now_ms = int(self.clock() * 1000)
        entry = self._data.get(session_hex)
        if entry is None:
            entry = {"anchor_ms": now_ms, "seen_ms": now_ms}
            self._data[session_hex] = entry
            log.warning("session %s has no wall-clock time (recorded before a watch reboot "
                        "with the clock unset); anchoring it at the relay's clock", session_hex)
        else:
            entry["seen_ms"] = now_ms
        self._save()
        return entry["anchor_ms"]
