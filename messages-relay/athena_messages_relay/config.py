"""The relay's settings: a JSON file the ``config`` command writes."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path.home() / "Library" / "Application Support" / "Athena Messages"
LOG_DIR = Path.home() / "Library" / "Logs" / "Athena Messages"
DEFAULT_CHAT_DB = str(Path.home() / "Library" / "Messages" / "chat.db")
DEFAULT_CONTACTS = str(Path.home() / "Library" / "Application Support" / "AddressBook")


def config_path() -> Path:
    """``ATHENA_MESSAGES_CONFIG`` or the default file."""
    env = os.environ.get("ATHENA_MESSAGES_CONFIG")
    return Path(env) if env else APP_DIR / "config.json"


@dataclasses.dataclass
class Config:
    #: the full ingest URL: ``http://<server>:8000/v1/ingest/messages`` (ingest token) or
    #: ``http://<server>:8001/ingest/messages`` (MCP token, through the relay routes)
    url: str = ""
    token: str = ""
    #: first day to send (YYYY-MM-DD, the Mac's local time); None = all of the history
    since: Optional[str] = None
    #: chats left out: a chat id, a group's name, or a participant's handle or name
    exclude: List[str] = dataclasses.field(default_factory=list)
    include_groups: bool = True
    batch_size: int = 500
    poll_seconds: float = 15.0
    chat_db: str = DEFAULT_CHAT_DB
    contacts_dir: str = DEFAULT_CONTACTS

    def missing(self) -> List[str]:
        return [k for k in ("url", "token") if not getattr(self, k)]


def load(path: Optional[Path] = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in dataclasses.fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save(cfg: Config, path: Optional[Path] = None) -> Path:
    """Write ``cfg`` atomically, readable by the owner only (it holds the token)."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=2)
    os.replace(str(tmp), str(path))
    return path
