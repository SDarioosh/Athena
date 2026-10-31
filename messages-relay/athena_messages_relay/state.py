"""What the server has accepted so far, kept next to the config.

Written atomically after each accepted upload, so a crash or a failed upload sends the
same batch again (the server ignores what it already has)."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
from pathlib import Path
from typing import Dict, Iterator, List


@dataclasses.dataclass
class State:
    #: the highest message ROWID the server accepted (or that was left out on purpose)
    last_rowid: int = 0
    #: the highest date_edited / date_retracted seen (Apple time, raw), macOS 13+
    edit_cursor: int = 0
    #: chats whose removal the server confirmed (``DELETE .../chats/...``)
    deleted_chats: List[str] = dataclasses.field(default_factory=list)
    #: chats to send again from the start (after ``include``): chat id -> last ROWID sent
    resend: Dict[str, int] = dataclasses.field(default_factory=dict)
    #: the name last sent for each handle, so a Contacts change is sent once
    names: Dict[str, str] = dataclasses.field(default_factory=dict)


def state_path(config_file: Path) -> Path:
    return config_file.with_name("state.json")


def load(path: Path) -> State:
    if not path.exists():
        return State()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in dataclasses.fields(State)}
    return State(**{k: v for k, v in data.items() if k in known})


def save(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(str(tmp), "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(state), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """One sync at a time (the agent and a ``once`` in a terminal)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path.with_name("relay.lock")), "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
