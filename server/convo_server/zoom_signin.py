"""Athena's Zoom sign-in: a join token for meetings in other Zoom accounts.

Since 2026-03-02 a Meeting SDK app may join a meeting hosted by another Zoom account only
on behalf of a signed-in Zoom user (developers.zoom.us/docs/meeting-sdk/obf-faq/). The
owner signs Athena in once with their own Zoom account (a dedicated Athena account with a
ZAK is a later option) through the Marketplace app's OAuth client
(``convo-server zoom-login``, authorization code with PKCE; the server listens for
nothing) and keeps only the refresh token, in ``<data dir>/zoom/refresh_token.json``
(mode 0600, replaced atomically).

On a join request, and only then, the launcher calls ``join_token``: it trades the refresh
token for an access token (Zoom rotates the refresh token on every use, so the new one is
saved before the access token is used) and asks Zoom for a ZAK (join as the signed-in account)
or an OBF token (on behalf of the signed-in user, who must be in the meeting; it is for
one meeting and lasts about JOIN_TOKEN_SECONDS). These are
the only network calls, to Zoom's own API, with a short timeout; no audio, transcript or
other data goes there. Nothing refreshes in the background. Tokens are never logged.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import Settings

AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
TOKEN_URL = "https://zoom.us/oauth/token"
API_URL = "https://api.zoom.us/v2"
TIMEOUT = httpx.Timeout(5.0)  # per call; two calls stay under the relay's 15 s
MODES = ("auto", "zak", "obf", "none")
LOGIN_HINT = "run convo-server zoom-login"
# How long a ZAK or OBF token lasts (Zoom: about 2 hours); the bot must join within it.
JOIN_TOKEN_SECONDS = 7200


class SigninNeeded(Exception):
    """No usable sign-in: missing, expired or revoked (HTTP 503 zoom_signin_needed)."""


class ZoomUnavailable(Exception):
    """Zoom could not be reached or refused for another reason (HTTP 502)."""


def token_file(settings: Settings) -> Path:
    return settings.data_dir / "zoom" / "refresh_token.json"


def signed_in(settings: Settings) -> bool:
    return token_file(settings).is_file()


def join_mode(settings: Settings) -> str:
    """``zak``, ``obf`` or ``none`` for the next join. ``auto`` (the default) is ``obf``
    once signed in (the owner's own Zoom account, owner's decision 2026-10-27), else
    ``none`` (meetings in the app's own account only)."""
    mode = settings.zoom_join_token
    if mode == "auto":
        return "obf" if signed_in(settings) else "none"
    return mode


def _client(settings: Settings) -> tuple[str, str]:
    if not (settings.zoom_client_id and settings.zoom_client_secret):
        raise SigninNeeded("Zoom sign-in needs CONVO_ZOOM_CLIENT_ID and "
                           "CONVO_ZOOM_CLIENT_SECRET (the Marketplace app's OAuth client)")
    return settings.zoom_client_id, settings.zoom_client_secret


def _basic(client_id: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def save_refresh_token(settings: Settings, token: str, scope: str | None = None) -> None:
    """Write the refresh token atomically, readable by this user only."""
    path = token_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    data = json.dumps({"refresh_token": token, "scope": scope, "saved_at": time.time()})
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_refresh_token(settings: Settings) -> str:
    try:
        data = json.loads(token_file(settings).read_text())
        return str(data["refresh_token"])
    except FileNotFoundError as e:
        raise SigninNeeded(f"Athena is not signed in to Zoom: {LOGIN_HINT}") from e
    except (ValueError, KeyError, TypeError) as e:
        raise SigninNeeded(f"Athena's Zoom sign-in file is damaged: {LOGIN_HINT}") from e


# --------------------------------------------------------------------- login
@dataclass(frozen=True)
class LoginRequest:
    url: str  # open it, sign in with the owner's Zoom account, allow
    state: str
    verifier: str  # PKCE code verifier, kept in memory until the code comes back


def start_login(settings: Settings) -> LoginRequest:
    client_id, _ = _client(settings)
    if not settings.zoom_redirect_url:
        raise SigninNeeded("Zoom sign-in needs CONVO_ZOOM_REDIRECT_URL (the app's "
                           "redirect URL, as entered in the Zoom Marketplace)")
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({"response_type": "code", "client_id": client_id,
                       "redirect_uri": settings.zoom_redirect_url, "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
    return LoginRequest(f"{AUTHORIZE_URL}?{query}", state, verifier)


def code_from_paste(pasted: str, state: str) -> str:
    """The code from what the owner pasted after Zoom's sign-in.

    - A redirect URL or its query (anything with ``://``, ``?``, ``=`` or ``&``): an
      ``error`` in it is an error, and its ``state`` must be present and equal ``state``
      (missing or empty is refused too), then its ``code`` is taken.
    - A bare code (none of those characters) is taken as it is: there is no state to
      check, and PKCE ties the code to this sign-in attempt anyway (Zoom only trades it
      with this attempt's verifier).
    """
    text = pasted.strip()
    if not text:
        raise ValueError("nothing pasted")
    if not any(c in text for c in ("://", "?", "=", "&")):
        if any(c.isspace() for c in text):
            raise ValueError("paste the whole address the browser went to, or its code")
        return text
    raw = urlsplit(text).query if "://" in text else text.split("?", 1)[-1]
    query = parse_qs(raw, keep_blank_values=True)
    error = (query.get("error") or [""])[0]
    if "error" in query:
        raise ValueError(f"Zoom refused the sign-in: {error or 'error'}")
    got = (query.get("state") or [""])[0]
    if state and got != state:
        if not got:
            raise ValueError("the pasted URL has no state; paste the whole address the "
                             "browser went to")
        raise ValueError("the pasted URL is from another sign-in attempt (state differs)")
    code = (query.get("code") or [""])[0]
    if not code:
        raise ValueError("no code= in the pasted URL")
    return code


async def finish_login(settings: Settings, login: LoginRequest, code: str, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Trade the code for tokens and save the refresh token. Returns the granted scopes."""
    client_id, secret = _client(settings)
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": settings.zoom_redirect_url or "",
            "code_verifier": login.verifier}
    data = await _token_call(client_id, secret, form, transport)
    save_refresh_token(settings, data["refresh_token"], data.get("scope"))
    return str(data.get("scope") or "")


# ---------------------------------------------------------------- join token
async def join_token(settings: Settings, kind: str, meeting_id: str, *,
                     transport: httpx.AsyncBaseTransport | None = None) -> str:
    """A fresh ZAK (``kind`` zak) or OBF token (``obf``) for ``meeting_id``.

    Raises SigninNeeded (missing or expired sign-in) or ZoomUnavailable."""
    client_id, secret = _client(settings)
    refresh = load_refresh_token(settings)
    data = await _token_call(client_id, secret,
                             {"grant_type": "refresh_token", "refresh_token": refresh},
                             transport)
    # Zoom rotates the refresh token: keep the new one before anything else can fail
    save_refresh_token(settings, data["refresh_token"], data.get("scope"))
    params = {"type": "zak"} if kind == "zak" else \
        {"type": "onbehalf", "meeting_id": meeting_id}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as http:
            r = await http.get(f"{API_URL}/users/me/token", params=params,
                               headers={"Authorization": f"Bearer {data['access_token']}"})
    except httpx.HTTPError as e:
        raise ZoomUnavailable(f"could not reach Zoom: {type(e).__name__}") from None
    if r.status_code == 401:
        raise SigninNeeded(f"Zoom refused Athena's sign-in: {LOGIN_HINT}")
    if r.status_code != 200:
        raise ZoomUnavailable(f"Zoom would not give a {kind} token "
                              f"(HTTP {r.status_code}{_zoom_reason(r)})")
    token = _json(r).get("token")
    if not token:
        raise ZoomUnavailable(f"Zoom sent no {kind} token")
    return str(token)


async def _token_call(client_id: str, secret: str, form: dict[str, str],
                      transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as http:
            r = await http.post(TOKEN_URL, data=form, headers=_basic(client_id, secret))
    except httpx.HTTPError as e:
        raise ZoomUnavailable(f"could not reach Zoom: {type(e).__name__}") from None
    data = _json(r)
    if r.status_code == 200 and data.get("access_token") and data.get("refresh_token"):
        return data
    if data.get("error") == "invalid_client":
        raise SigninNeeded("Zoom rejected CONVO_ZOOM_CLIENT_ID / CONVO_ZOOM_CLIENT_SECRET")
    if r.status_code in (400, 401):
        what = "sign-in" if form["grant_type"] == "authorization_code" else "sign-in expired"
        raise SigninNeeded(f"Athena's Zoom {what}{_zoom_reason(r)}: {LOGIN_HINT}")
    raise ZoomUnavailable(f"Zoom's token service answered HTTP {r.status_code}"
                          f"{_zoom_reason(r)}")


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _zoom_reason(r: httpx.Response) -> str:
    """Zoom's error words (never a token: error responses carry none)."""
    data = _json(r)
    reason = data.get("reason") or data.get("message") or data.get("error")
    return f" ({str(reason)[:120]})" if reason else ""
