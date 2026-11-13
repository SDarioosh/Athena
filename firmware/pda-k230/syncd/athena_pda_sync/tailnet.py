"""The tailnet state for the UI's `tailnet` message, from `tailscale status --json`.

`BackendState` -> `state`:

| BackendState | state | url |
|---|---|---|
| `Running` | `running` | null |
| `NeedsLogin` | `needs_login` | `AuthURL` (or the link `tailscale login` printed), else null |
| `NeedsMachineAuth` | `needs_login` | null (an admin must approve the device in the tailnet) |
| `Stopped` | `stopped` | null |
| `NoState`, `Starting`, anything else | `unknown` | null |

- `tailscale` not installed, a timeout, or output that isn't JSON: `unknown`.
- `tailscale status` failing with no JSON (tailscaled not running): `stopped`.
- `name`: `Self.DNSName` without the final dot (`athena-pda.tail1234.ts.net`), else
  `Self.HostName`, else null.
- The login link: `status --json` has `AuthURL` once a login was started. When Tailscale
  needs a login and has no link, `Tailnet` starts `tailscale login` once in the background
  (if allowed) and also reads the link from its output, as a fallback.

The subprocess calls are injected (`run`, `start_login`) so tests fake them.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("athena_pda_sync.tailnet")

UNKNOWN = "unknown"
STOPPED = "stopped"
NEEDS_LOGIN = "needs_login"
RUNNING = "running"

STATUS_TIMEOUT = 10.0
LOGIN_RETRY = 60.0  # a `tailscale login` that ended without a link is started again after
_LINK = re.compile(r"https://\S+")


@dataclass(frozen=True)
class TailnetState:
    state: str = UNKNOWN
    url: str | None = None
    name: str | None = None

    def message(self) -> dict[str, Any]:
        return {"t": "tailnet", "state": self.state, "url": self.url, "name": self.name}


# (exit code, stdout); raises FileNotFoundError when tailscale isn't installed and
# subprocess.TimeoutExpired on a timeout
Runner = Callable[[list[str], float], tuple[int, str]]


class LoginProcess(Protocol):
    def running(self) -> bool: ...

    def link(self) -> str | None: ...


def run_subprocess(argv: list[str], timeout: float) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout


class _Login:
    """`tailscale login` in the background; remembers the first https link it prints."""

    def __init__(self, argv: list[str]) -> None:
        self._link: str | None = None
        self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            m = _LINK.search(line)
            if m and self._link is None:
                self._link = m.group(0)

    def running(self) -> bool:
        return self._proc.poll() is None

    def link(self) -> str | None:
        return self._link


def start_login_subprocess(argv: list[str]) -> LoginProcess:
    return _Login(argv)


def parse_status(code: int, stdout: str) -> TailnetState:
    """The state from `tailscale status --json`'s exit code and output."""
    try:
        obj = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        return TailnetState(STOPPED if code != 0 and not stdout.strip() else UNKNOWN)
    backend = obj.get("BackendState")
    me = obj.get("Self") if isinstance(obj.get("Self"), dict) else {}
    name = me.get("DNSName") if isinstance(me.get("DNSName"), str) else ""
    name = name.rstrip(".") or (me.get("HostName") if isinstance(me.get("HostName"), str)
                                else "") or None
    if backend == "Running":
        return TailnetState(RUNNING, None, name)
    if backend == "NeedsLogin":
        url = obj.get("AuthURL")
        return TailnetState(NEEDS_LOGIN, url if isinstance(url, str) and url else None, name)
    if backend == "NeedsMachineAuth":
        return TailnetState(NEEDS_LOGIN, None, name)
    if backend == "Stopped":
        return TailnetState(STOPPED, None, name)
    return TailnetState(UNKNOWN, None, name)


class Tailnet:
    def __init__(self, *, binary: str = "tailscale", run: Runner = run_subprocess,
                 start_login: Callable[[list[str]], LoginProcess] = start_login_subprocess,
                 login: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.binary = binary
        self._clock = clock
        self._login_at: float | None = None
        self._run = run
        self._start_login = start_login
        self.login = login
        self._login: LoginProcess | None = None
        self._missing_logged = False

    def poll(self) -> TailnetState:
        """Blocking (call it in a thread)."""
        try:
            code, out = self._run([self.binary, "status", "--json"], STATUS_TIMEOUT)
        except FileNotFoundError:
            if not self._missing_logged:
                log.warning("tailscale is not installed; tailnet state unknown")
                self._missing_logged = True
            return TailnetState()
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("tailscale status failed: %s", type(e).__name__)
            return TailnetState()
        state = parse_status(code, out)
        if state.state == NEEDS_LOGIN and state.url is None:
            state = self._login_link(state, out)
        elif state.state != NEEDS_LOGIN:
            self._login, self._login_at = None, None
        return state

    def _login_link(self, state: TailnetState, out: str) -> TailnetState:
        if '"NeedsMachineAuth"' in out:
            return state  # nothing to log in to: an admin approves the device
        if self._login is not None:
            link = self._login.link()
            if link:
                return TailnetState(NEEDS_LOGIN, link, state.name)
            if self._login.running():
                return state
        if not self.login:
            return state
        if self._login_at is not None and self._clock() - self._login_at < LOGIN_RETRY:
            return state
        self._login_at = self._clock()
        try:
            self._login = self._start_login([self.binary, "login"])
            log.info("Tailscale needs a login: started tailscale login for the link")
        except OSError as e:
            log.warning("can't start tailscale login: %s", type(e).__name__)
        return state
