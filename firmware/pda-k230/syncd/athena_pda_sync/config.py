"""Paths and `config.json` (docs/k230-pda-plan.md "Storage").

- `$ATHENA_PDA_DIR` (default `/var/lib/athena-pda`): syncd's `token` (pairing.TokenStore),
  `pda.json` (`pda_sync.PdaState`: last pushed feed, cal_add dedupe, last good weights) and
  `config.json`, beside the UI's own files.
- `$ATHENA_PDA_RUN` (default `/run/athena-pda`): the UI's `glance.sock`.

`config.json` (every key optional; a bad value is logged and the default used):

```json
{"tz": "Europe/Berlin", "calendar_href": "<the CalDAV calendar's id>",
 "templates": ["Call", "Meeting", "Reminder"], "proxy": "http://127.0.0.1:1055",
 "tailscale": true}
```

- `tz`: IANA zone for TIME and the feed (default UTC);
- `calendar_href`: where new events go (without it the hub takes the owner's first
  calendar);
- `templates`: up to 12 event titles for the "+" picker (a list, or "Call,Meeting");
- `proxy`: an `http://host:port` proxy for every hub request (Tailscale's userspace mode);
  none by default (the environment's proxy variables are not used);
- `tailscale`: report the tailnet state to the UI (default true). syncd only reads it;
  the login is started by root on the image (`athena-tailnet watch`). An old
  `tailscale_login` key is ignored.

The file is read again before a sync when it changed.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena_relay.pda_feed import resolve_tz

log = logging.getLogger("athena_pda_sync.config")

DATA_ENV = "ATHENA_PDA_DIR"
RUN_ENV = "ATHENA_PDA_RUN"
DEFAULT_DATA = "/var/lib/athena-pda"
DEFAULT_RUN = "/run/athena-pda"
SOCKET = "glance.sock"


@dataclass(frozen=True)
class Paths:
    data: Path
    run: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Paths:
        env = os.environ if env is None else env
        return cls(Path(env.get(DATA_ENV) or DEFAULT_DATA),
                   Path(env.get(RUN_ENV) or DEFAULT_RUN))

    @property
    def socket(self) -> Path:
        return self.run / SOCKET

    @property
    def token(self) -> Path:
        return self.data / "token"

    @property
    def state(self) -> Path:
        return self.data / "pda.json"

    @property
    def config(self) -> Path:
        return self.data / "config.json"


@dataclass(frozen=True)
class Config:
    tz: str = "UTC"
    calendar_href: str | None = None
    templates: list[str] = field(default_factory=list)
    proxy: str | None = None
    tailscale: bool = True


def check_proxy(value: Any) -> str | None:
    """`http://host:port` (or None for no proxy). Raises ValueError."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("proxy must be a string")
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("proxy must be http://host:port")
    parts.port  # noqa: B018 (raises ValueError for a bad port)
    return value.strip().rstrip("/")


def parse(raw: Any) -> Config:
    """A Config from the decoded JSON; bad values are logged and left at their default."""
    if not isinstance(raw, dict):
        log.warning("config.json is not a JSON object; using the defaults")
        return Config()
    out: dict[str, Any] = {}
    tz = raw.get("tz")
    if tz is not None:
        try:
            if not isinstance(tz, str):
                raise ValueError("tz must be a string")
            resolve_tz(tz)
            out["tz"] = tz
        except ValueError as e:
            log.warning("config.json tz: %s; using UTC", e)
    href = raw.get("calendar_href")
    if isinstance(href, str) and href.strip():
        out["calendar_href"] = href.strip()
    elif href not in (None, ""):
        log.warning("config.json calendar_href must be a string; ignored")
    templates = raw.get("templates")
    if isinstance(templates, str):
        templates = templates.split(",")
    if isinstance(templates, list):
        out["templates"] = [t.strip() for t in templates if isinstance(t, str) and t.strip()]
    elif templates is not None:
        log.warning("config.json templates must be a list of titles; ignored")
    try:
        out["proxy"] = check_proxy(raw.get("proxy"))
    except ValueError as e:
        log.warning("config.json proxy: %s; no proxy", e)
    for key in ("tailscale",):
        if key in raw:
            if isinstance(raw[key], bool):
                out[key] = raw[key]
            else:
                log.warning("config.json %s must be true or false; ignored", key)
    return Config(**out)


class ConfigFile:
    """`config.json`, read again when its mtime or size changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self.config = Config()
        self.reload()

    def reload(self) -> Config:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            if self._stamp is not None:
                log.info("config.json is gone; using the defaults")
            self._stamp, self.config = None, Config()
            return self.config
        except OSError as e:
            log.warning("can't stat config.json (%s); keeping the last settings", e)
            return self.config
        if stamp == self._stamp:
            return self.config
        self._stamp = stamp
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("can't read config.json (%s); using the defaults", e)
            self.config = Config()
            return self.config
        self.config = parse(raw)
        log.info("config: tz=%s calendar=%s templates=%d proxy=%s", self.config.tz,
                 "set" if self.config.calendar_href else "default",
                 len(self.config.templates), "set" if self.config.proxy else "none")
        return self.config
