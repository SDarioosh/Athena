"""The tailnet state for the UI's `tailnet` message, from `tailscale status --json`.

`BackendState` -> `state`:

| BackendState | state | url |
|---|---|---|
| `Running` | `running` | null |
| `NeedsLogin` | `needs_login` | `AuthURL`, else null |
| `NeedsMachineAuth` | `needs_login` | null (an admin must approve the device in the tailnet) |
| `Stopped` | `stopped` | null |
| `NoState`, `Starting`, anything else | `unknown` | null |

- `tailscale` not installed, a timeout, or output that isn't JSON: `unknown`.
- `tailscale status` failing with no JSON (tailscaled not running): `stopped`.
- `name`: `Self.DNSName` without the final dot (`athena-pda.tail1234.ts.net`), else
  `Self.HostName`, else null.
- Read only: syncd is no Tailscale operator and never starts a login or changes a
  setting (any user may run `status`; checked with tailscale 1.104.7). On the image
  root's `athena-tailnet watch` starts `tailscale login` when one is needed; the link
  then shows up here as `AuthURL`.

The subprocess call is injected (`run`) so tests fake it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("athena_pda_sync.tailnet")

UNKNOWN = "unknown"
STOPPED = "stopped"
NEEDS_LOGIN = "needs_login"
RUNNING = "running"

STATUS_TIMEOUT = 10.0


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


def run_subprocess(argv: list[str], timeout: float) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout


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
    def __init__(self, *, binary: str = "tailscale", run: Runner = run_subprocess) -> None:
        self.binary = binary
        self._run = run
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
        return parse_status(code, out)
