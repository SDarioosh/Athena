"""Pairing with Odysseus by the hub's "Pair a phone" code (docs/k230-pda-plan.md).

The QR code holds `odysseus-pair:v1?url=<Odysseus URL>&code=<pairing code>`, no token.
The code is single use and valid for 10 minutes; `redeem` trades it at
`POST <url>/api/pair/redeem` {"code"} for this device's own `ody_…` token (the hub names it
"Phone relay <date>"; revoking it in the hub's Settings unpairs the device).

- `parse_link` reads the raw QR text (what the vision process hands over), `check_url` and
  `check_code` the pieces the UI sends when the code is typed.
- `TokenStore` keeps `{"url", "token", "name"}` in `$ATHENA_PDA_DIR/token`, mode 0600,
  written atomically (a temporary file, fsync, rename). The token is never logged.

The rules and messages follow the Android relay (`net/Pairing.kt`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .hub import make_opener

log = logging.getLogger("athena_pda_sync.pairing")

PREFIX = "odysseus-pair:v1?"
REDEEM = "/api/pair/redeem"
CODE = re.compile(r"[A-Za-z0-9_-]{8,100}")
URL_MAX = 300
MAKE_NEW = "In Odysseus open Settings → Integrations → Pair a phone."


class PairError(Exception):
    """A pairing that can't go on; the message is one short line for the UI."""


@dataclass(frozen=True)
class Paired:
    url: str
    token: str
    name: str = ""

    def __repr__(self) -> str:  # never the token
        return f"Paired({self.url!r}, {self.name!r})"

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).hostname or self.url


def check_url(raw: str) -> str:
    """The hub's base URL: http(s), a host, no user, query or fragment; no trailing slash."""
    text = (raw or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(text)
    try:
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        port_ok = False
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok:
        raise PairError("The Odysseus address must start with http:// or https:// "
                        "and name a host.")
    if "@" in parts.netloc or parts.query or parts.fragment or "?" in text or "#" in text:
        raise PairError("The Odysseus address can't have a user, ?query or #part.")
    if len(text) > URL_MAX:
        raise PairError("The Odysseus address is too long.")
    if any(c.isspace() for c in text):
        raise PairError("The Odysseus address can't contain spaces.")
    return text


def check_code(raw: str) -> str:
    code = (raw or "").strip()
    if not CODE.fullmatch(code):
        raise PairError("That is not a pairing code. " + MAKE_NEW)
    return code


def parse_link(raw: str) -> tuple[str, str]:
    """(url, code) from `odysseus-pair:v1?url=…&code=…`. Raises PairError."""
    text = (raw or "").strip()
    if not text.startswith(PREFIX):
        raise PairError("Not an Odysseus pairing code. " + MAKE_NEW)
    fields: dict[str, str] = {}
    for part in text[len(PREFIX):].split("&"):
        key, _, value = part.partition("=")
        if key and key not in fields:
            try:
                fields[key] = urllib.parse.unquote_plus(value, errors="strict")
            except UnicodeDecodeError:
                raise PairError("The pairing link is damaged.") from None
    code = fields.get("code", "")
    if not CODE.fullmatch(code):
        raise PairError("The pairing link has no valid code.")
    try:
        url = check_url(fields.get("url", ""))
    except PairError as e:
        raise PairError(f"Odysseus address in the code: {e}") from None
    return url, code


def redeem(url: str, code: str, *, proxy: str | None = None, timeout: float = 20.0,
           opener: urllib.request.OpenerDirector | None = None) -> Paired:
    """POST the code to the hub; return the new token. Raises PairError. The code is used
    up by the first try that reaches the hub, so a failure means making a new code."""
    url, code = check_url(url), check_code(code)
    opener = opener or make_opener(proxy)
    req = urllib.request.Request(url + REDEEM, data=json.dumps({"code": code}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        status, body = e.code, _read(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        host = urllib.parse.urlsplit(url).hostname
        raise PairError(f"Odysseus not reachable at {host} ({reason}). "
                        "Is Tailscale up?") from None
    if status == 200:
        return _paired(url, body)
    if status == 429:
        raise PairError("Too many tries. Wait a few minutes, then make a new code.")
    if 300 <= status < 400:
        raise PairError(f"Odysseus answered with a redirect (HTTP {status}); "
                        "check the address.")
    if status in (404, 405):
        raise PairError("This Odysseus can't pair devices yet (update the hub).")
    if 400 <= status < 500:
        detail = _detail(body)
        raise PairError(detail or f"The code was not accepted (HTTP {status}). "
                                  "Make a new one.")
    raise PairError(f"Odysseus error HTTP {status}; try again with a new code.")


def _read(e: urllib.error.HTTPError) -> bytes:
    try:
        return e.read(64 * 1024)
    except Exception:  # only for the message
        return b""


def _detail(body: bytes) -> str:
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return ""
    return " ".join(detail.split())[:200] if isinstance(detail, str) else ""


def _paired(url: str, body: bytes) -> Paired:
    try:
        obj = json.loads(body)
    except ValueError:
        raise PairError("Odysseus answered without JSON.") from None
    token = obj.get("token") if isinstance(obj, dict) else None
    if not isinstance(token, str) or not token.startswith("ody_") or len(token) < 12 \
            or any(c.isspace() for c in token):
        raise PairError("Odysseus answered without a token.")
    name = obj.get("name")
    return Paired(url, token, name if isinstance(name, str) else "")


# ------------------------------------------------------------------ token file


class TokenStore:
    """`$ATHENA_PDA_DIR/token`: `{"url", "token", "name"}`, mode 0600."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Paired | None:
        try:
            st = self.path.stat()
            raw = self.path.read_text("utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("can't read the token file (%s); unpaired", e.strerror or e)
            return None
        if stat.S_IMODE(st.st_mode) & 0o077:
            log.warning("the token file was readable by others; setting mode 0600")
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        try:
            obj = json.loads(raw)
            paired = Paired(check_url(obj["url"]), str(obj["token"]), str(obj.get("name", "")))
        except (ValueError, KeyError, TypeError, AttributeError, PairError):
            log.warning("the token file is damaged; unpaired")
            return None
        if not paired.token or any(c.isspace() for c in paired.token):
            log.warning("the token file is damaged; unpaired")
            return None
        return paired

    def save(self, paired: Paired) -> None:
        """Write it atomically, mode 0600 from the start. Raises OSError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        data = json.dumps({"url": paired.url, "token": paired.token,
                           "name": paired.name}).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)  # whatever the umask
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)

    def delete(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        _fsync_dir(self.path.parent)
        return True


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
