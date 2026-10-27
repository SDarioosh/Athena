"""Athena's Zoom sign-in (convo_server/zoom_signin.py) against a fake Zoom OAuth and API.

No network: every call goes to FakeZoom, an httpx transport. All ids, keys and tokens
are fictional.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_meetings import _bot, _report, _wait

from convo_server import zoom_signin
from convo_server.cli import zoom_login

CLIENT_ID, CLIENT_SECRET = "fictional-client-id", "fictional-client-secret"
REDIRECT = "https://athena.example/zoom/callback"


class FakeZoom:
    """Zoom's token endpoint and /users/me/token, with rotating refresh tokens."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.valid_refresh = {"refresh-0"}
        self.access: set[str] = set()
        self.n = 0
        self.down = False
        self.api_status = 200
        self.verifier: str | None = None

    def _tokens(self) -> httpx.Response:
        self.n += 1
        refresh, access = f"refresh-{self.n}", f"access-{self.n}"
        self.valid_refresh.add(refresh)
        self.access.add(access)
        return httpx.Response(200, json={"access_token": access, "refresh_token": refresh,
                                         "token_type": "bearer", "expires_in": 3599,
                                         "scope": "user:read:token user:read:zak"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        if request.url.path == "/oauth/token":
            auth = base64.b64decode(request.headers["Authorization"].split()[1]).decode()
            if auth != f"{CLIENT_ID}:{CLIENT_SECRET}":
                return httpx.Response(401, json={"reason": "Invalid client_id or "
                                                 "client_secret", "error": "invalid_client"})
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                self.verifier = form["code_verifier"][0]
                if form["code"] != ["good-code"] or form["redirect_uri"] != [REDIRECT]:
                    return httpx.Response(400, json={"reason": "Invalid authorization code",
                                                     "error": "invalid_request"})
                return self._tokens()
            token = form["refresh_token"][0]
            if token not in self.valid_refresh:
                return httpx.Response(400, json={"reason": "Invalid Token!",
                                                 "error": "invalid_request"})
            self.valid_refresh.discard(token)  # Zoom rotates refresh tokens on use
            return self._tokens()
        if request.url.path == "/v2/users/me/token":
            if request.headers.get("Authorization", "").split()[-1] not in self.access:
                return httpx.Response(401, json={"code": 124, "message": "Invalid token."})
            if self.api_status != 200:
                return httpx.Response(self.api_status, json={"code": 300,
                                                             "message": "Not allowed"})
            q = request.url.params
            if q["type"] == "zak":
                return httpx.Response(200, json={"token": "fictional-zak"})
            assert q["type"] == "onbehalf" and q["meeting_id"]
            return httpx.Response(200, json={"token": f"fictional-obf-{q['meeting_id']}"})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def zoom() -> FakeZoom:
    return FakeZoom()


@pytest.fixture
def zsettings(settings):
    settings.zoom_client_id, settings.zoom_client_secret = CLIENT_ID, CLIENT_SECRET
    settings.zoom_redirect_url = REDIRECT
    return settings


def _signed_in(settings, token: str = "refresh-0") -> None:
    zoom_signin.save_refresh_token(settings, token)


def _stored(settings) -> str:
    return json.loads(zoom_signin.token_file(settings).read_text())["refresh_token"]


# --------------------------------------------------------------------- login
def test_login_url_has_pkce_and_state(zsettings) -> None:
    login = zoom_signin.start_login(zsettings)
    q = parse_qs(urlsplit(login.url).query)
    assert login.url.startswith("https://zoom.us/oauth/authorize?")
    assert q["client_id"] == [CLIENT_ID] and q["redirect_uri"] == [REDIRECT]
    assert q["response_type"] == ["code"] and q["state"] == [login.state]
    assert q["code_challenge_method"] == ["S256"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(login.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [challenge]
    assert CLIENT_SECRET not in login.url


def test_login_needs_settings(settings) -> None:
    with pytest.raises(zoom_signin.SigninNeeded, match="CONVO_ZOOM_CLIENT_ID"):
        zoom_signin.start_login(settings)
    settings.zoom_client_id, settings.zoom_client_secret = CLIENT_ID, CLIENT_SECRET
    with pytest.raises(zoom_signin.SigninNeeded, match="CONVO_ZOOM_REDIRECT_URL"):
        zoom_signin.start_login(settings)


def test_code_from_paste() -> None:
    assert zoom_signin.code_from_paste(f"{REDIRECT}?code=abc&state=s1", "s1") == "abc"
    assert zoom_signin.code_from_paste(f"  {REDIRECT}?code=abc  ", "s1") == "abc"
    assert zoom_signin.code_from_paste("abc", "s1") == "abc"
    assert zoom_signin.code_from_paste("code=abc&state=s1", "s1") == "abc"
    with pytest.raises(ValueError, match="another sign-in"):
        zoom_signin.code_from_paste(f"{REDIRECT}?code=abc&state=other", "s1")
    with pytest.raises(ValueError, match="access_denied"):
        zoom_signin.code_from_paste(f"{REDIRECT}?error=access_denied&state=s1", "s1")
    with pytest.raises(ValueError, match="no code"):
        zoom_signin.code_from_paste(f"{REDIRECT}?state=s1", "s1")
    with pytest.raises(ValueError):
        zoom_signin.code_from_paste("  ", "s1")


def test_login_stores_refresh_token_0600(zsettings, zoom) -> None:
    login = zoom_signin.start_login(zsettings)
    scope = asyncio.run(zoom_signin.finish_login(zsettings, login, "good-code",
                                                 transport=zoom.transport))
    assert "user:read:token" in scope
    assert zoom.verifier == login.verifier  # PKCE
    path = zoom_signin.token_file(zsettings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert _stored(zsettings) == "refresh-1"
    assert list(path.parent.iterdir()) == [path]  # no temp file left
    assert zoom_signin.join_mode(zsettings) == "zak"  # auto: zak once signed in


def test_login_bad_code(zsettings, zoom) -> None:
    login = zoom_signin.start_login(zsettings)
    with pytest.raises(zoom_signin.SigninNeeded, match="authorization code"):
        asyncio.run(zoom_signin.finish_login(zsettings, login, "bad",
                                             transport=zoom.transport))
    assert not zoom_signin.signed_in(zsettings)


def test_cli_login(zsettings, zoom, capsys) -> None:
    prompts = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "good-code"

    assert zoom_login(zsettings, ask=ask, transport=zoom.transport) == 0
    out = capsys.readouterr().out
    assert "https://zoom.us/oauth/authorize?" in out and "Signed in" in out
    assert "refresh-1" not in out and "access-1" not in out  # tokens never printed
    assert CLIENT_SECRET not in out
    assert _stored(zsettings) == "refresh-1"
    assert zoom_login(zsettings, status_only=True) == 0
    assert "signed in" in capsys.readouterr().out


def test_cli_login_refused(zsettings, zoom, capsys) -> None:
    zsettings.zoom_client_secret = "wrong"
    assert zoom_login(zsettings, ask=lambda _: "good-code", transport=zoom.transport) == 1
    assert "CONVO_ZOOM_CLIENT_SECRET" in capsys.readouterr().out


# ---------------------------------------------------------------- join token
def test_join_refreshes_rotates_and_gets_zak(zsettings, zoom, caplog) -> None:
    _signed_in(zsettings)
    caplog.set_level(logging.DEBUG)
    token = asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                               transport=zoom.transport))
    assert token == "fictional-zak"
    assert _stored(zsettings) == "refresh-1"  # the rotated token was saved
    assert "refresh-0" not in zoom.valid_refresh
    assert [c.url.path for c in zoom.calls] == ["/oauth/token", "/v2/users/me/token"]
    assert zoom.calls[1].headers["Authorization"] == "Bearer access-1"
    # next join uses the new refresh token
    asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                       transport=zoom.transport))
    assert _stored(zsettings) == "refresh-2"
    for secret in ("refresh-0", "refresh-1", "access-1", "fictional-zak", CLIENT_SECRET):
        assert secret not in caplog.text  # never logged


def test_join_obf_names_the_meeting(zsettings, zoom) -> None:
    _signed_in(zsettings)
    token = asyncio.run(zoom_signin.join_token(zsettings, "obf", "81234567890",
                                               transport=zoom.transport))
    assert token == "fictional-obf-81234567890"
    assert dict(zoom.calls[1].url.params) == {"type": "onbehalf",
                                              "meeting_id": "81234567890"}


def test_rotation_saved_even_if_token_call_fails(zsettings, zoom) -> None:
    _signed_in(zsettings)
    zoom.api_status = 400
    with pytest.raises(zoom_signin.ZoomUnavailable, match="Not allowed"):
        asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                           transport=zoom.transport))
    assert _stored(zsettings) == "refresh-1"


def test_expired_signin(zsettings, zoom) -> None:
    _signed_in(zsettings, "refresh-old")
    with pytest.raises(zoom_signin.SigninNeeded, match="sign-in expired.*zoom-login"):
        asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                           transport=zoom.transport))
    assert _stored(zsettings) == "refresh-old"  # left for the owner to replace


def test_missing_signin_and_zoom_down(zsettings, zoom) -> None:
    with pytest.raises(zoom_signin.SigninNeeded, match="not signed in"):
        asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                           transport=zoom.transport))
    assert zoom.calls == []
    _signed_in(zsettings)
    zoom.down = True
    with pytest.raises(zoom_signin.ZoomUnavailable, match="could not reach Zoom"):
        asyncio.run(zoom_signin.join_token(zsettings, "zak", "123456789",
                                           transport=zoom.transport))


# ------------------------------------------------------------------- routes
@pytest.fixture
def zclient(zsettings, client, tmp_path, zoom, monkeypatch):
    zsettings.meeting_bot_path, _ = _bot(tmp_path, "record")
    zsettings.meeting_bot_runner = "plain"
    zsettings.zoom_sdk_key, zsettings.zoom_sdk_secret = "fictional-key", "fictional-secret"
    r = client.get("/v1/meetings")  # makes the launcher
    assert r.status_code == 200
    client.app.state.meetings.zoom_transport = zoom.transport
    return client


def _join(client, tmp_path, settings, mode: str = "record"):
    settings.meeting_bot_path, rep = _bot(tmp_path, mode)
    return client.post("/v1/meetings", json={
        "url": "https://acme.zoom.us/j/81234567890?pwd=Zz9fictional.1"}), rep


@pytest.mark.parametrize("mode, kind, token", [
    ("auto", "zak", "fictional-zak"), ("zak", "zak", "fictional-zak"),
    ("obf", "obf", "fictional-obf-81234567890")])
def test_join_passes_token_by_env(zclient, zsettings, zoom, tmp_path, mode, kind,
                                  token) -> None:
    _signed_in(zsettings)
    zsettings.zoom_join_token = mode
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    assert _wait(zclient, mid, "recording")["join_token_kind"] == kind
    info = _report(rep)
    assert info["env"]["ATHENA_ZOOM_JOIN_TOKEN"] == token
    assert info["env"]["ATHENA_ZOOM_JOIN_TOKEN_KIND"] == kind
    assert token not in " ".join(info["argv"])
    assert not any(v.startswith(("refresh-", "access-")) for v in info["env"].values())
    assert _stored(zsettings) == "refresh-1"
    zclient.delete(f"/v1/meetings/{mid}")


@pytest.mark.parametrize("mode", ["auto", "zak", "obf"])
def test_expired_signin_answers_503_and_starts_nothing(zclient, zsettings, tmp_path,
                                                       mode) -> None:
    _signed_in(zsettings, "refresh-old")
    zsettings.zoom_join_token = mode
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 503
    assert r.json()["detail"] == "zoom_signin_needed"
    assert "Athena's Zoom sign-in expired" in r.json()["message"]
    assert "convo-server zoom-login" in r.json()["message"]
    assert zclient.get("/v1/meetings").json()["meetings"] == []
    assert not rep.exists()  # the bot never started


@pytest.mark.parametrize("mode", ["zak", "obf"])
def test_signin_required_but_missing(zclient, zsettings, zoom, tmp_path, mode) -> None:
    zsettings.zoom_join_token = mode
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 503 and r.json()["detail"] == "zoom_signin_needed"
    assert "not signed in" in r.json()["message"]
    assert zoom.calls == [] and not rep.exists()


def test_zoom_unreachable_answers_502(zclient, zsettings, zoom, tmp_path) -> None:
    _signed_in(zsettings)
    zoom.down = True
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 502 and r.json()["detail"] == "zoom_unavailable"
    assert not rep.exists()


@pytest.mark.parametrize("mode, signed", [("none", True), ("auto", False)])
def test_no_token_mode_unchanged(zclient, zsettings, zoom, tmp_path, mode, signed) -> None:
    if signed:
        _signed_in(zsettings)
    zsettings.zoom_join_token = mode
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 202
    mid = r.json()["id"]
    assert _wait(zclient, mid, "recording")["join_token_kind"] == "none"
    env = _report(rep)["env"]
    assert "ATHENA_ZOOM_JOIN_TOKEN" not in env and "ATHENA_ZOOM_JOIN_TOKEN_KIND" not in env
    assert zoom.calls == []  # no network
    zclient.delete(f"/v1/meetings/{mid}")


def test_busy_asks_zoom_nothing(zclient, zsettings, zoom, tmp_path) -> None:
    _signed_in(zsettings)
    r, _ = _join(zclient, tmp_path, zsettings)
    calls = len(zoom.calls)
    r2, _ = _join(zclient, tmp_path, zsettings, "exit")
    assert r2.status_code == 409 and len(zoom.calls) == calls
    zclient.delete(f"/v1/meetings/{r.json()['id']}")


def test_bad_mode_is_501(zclient, zsettings, tmp_path) -> None:
    zsettings.zoom_join_token = "always"
    r, _ = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 501 and "CONVO_ZOOM_JOIN_TOKEN" in r.json()["detail"]


def test_no_network_without_a_meeting(zclient, zsettings, zoom) -> None:
    _signed_in(zsettings)
    for path in ("/v1/meetings", "/v1/sessions", "/health"):
        zclient.get(path)
    assert zoom.calls == []
