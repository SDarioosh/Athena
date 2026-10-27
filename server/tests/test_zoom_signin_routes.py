"""Zoom sign-in from Odysseus: /v1/zoom/signin* on the ingest API and the relay's
/zoom/signin* on the MCP HTTP app. Fake Zoom only (tests/test_zoom_signin.py FakeZoom),
no network; every id, secret and token is fictional.
"""

from __future__ import annotations

import json
import logging
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import TOKEN
from test_ingest_relay import MCP_TOKEN, mcp_client
from test_zoom_signin import CLIENT_ID, CLIENT_SECRET, REDIRECT, FakeZoom, _join, _stored

from convo_server import zoom_signin
from convo_server.api import create_app

Q = f"?access_token={MCP_TOKEN}"


@pytest.fixture
def zoom() -> FakeZoom:
    return FakeZoom()


@pytest.fixture
def zsettings(settings):
    settings.zoom_client_id, settings.zoom_client_secret = CLIENT_ID, CLIENT_SECRET
    settings.zoom_redirect_url = REDIRECT
    return settings


@pytest.fixture
def zc(zsettings, client, zoom):
    client.app.state.zoom_transport = zoom.transport
    return client


def _start(c) -> dict:
    r = c.post("/v1/zoom/signin/start")
    assert r.status_code == 200, r.text
    return r.json()


def _state(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def _redirect(url: str, code: str = "good-code") -> str:
    return f"{REDIRECT}?code={code}&state={_state(url)}"


# ------------------------------------------------------------------- status
def test_status_unconfigured(client) -> None:
    r = client.get("/v1/zoom/signin")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "signed_in": False, "join_token": "none",
                        "missing": ["CONVO_ZOOM_CLIENT_ID", "CONVO_ZOOM_CLIENT_SECRET",
                                    "CONVO_ZOOM_REDIRECT_URL"]}


def test_status_partly_configured(client, settings) -> None:
    settings.zoom_client_id = CLIENT_ID
    settings.zoom_redirect_url = "  "
    body = client.get("/v1/zoom/signin").json()
    assert body["configured"] is False
    assert body["missing"] == ["CONVO_ZOOM_CLIENT_SECRET", "CONVO_ZOOM_REDIRECT_URL"]


def test_status_configured_then_signed_in(zc, zsettings) -> None:
    assert zc.get("/v1/zoom/signin").json() == {
        "configured": True, "signed_in": False, "join_token": "none", "missing": []}
    zoom_signin.save_refresh_token(zsettings, "refresh-0")
    assert zc.get("/v1/zoom/signin").json() == {
        "configured": True, "signed_in": True, "join_token": "obf", "missing": []}
    zsettings.zoom_join_token = "zak"
    assert zc.get("/v1/zoom/signin").json()["join_token"] == "zak"
    zsettings.zoom_join_token = "always"  # a bad mode (joins answer 501): never echoed
    assert zc.get("/v1/zoom/signin").json()["join_token"] == "none"


def test_routes_need_the_ingest_token(zc) -> None:
    for method, path in (("GET", "/v1/zoom/signin"), ("POST", "/v1/zoom/signin/start"),
                         ("POST", "/v1/zoom/signin/finish")):
        r = zc.request(method, path, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


# -------------------------------------------------------------------- start
def test_start_keeps_a_private_pending_attempt(zc, zsettings) -> None:
    body = _start(zc)
    assert set(body) == {"url", "expires_in"} and body["expires_in"] == 600
    q = parse_qs(urlsplit(body["url"]).query)
    assert body["url"].startswith("https://zoom.us/oauth/authorize?")
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert q["client_id"] == [CLIENT_ID] and q["redirect_uri"] == [REDIRECT]
    path = zoom_signin.pending_file(zsettings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["state"] == q["state"][0]
    assert saved["verifier"] not in json.dumps(body)  # the verifier never leaves
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_new_start_replaces_the_old_one(zc, zsettings) -> None:
    first = _start(zc)
    second = _start(zc)
    assert _state(first["url"]) != _state(second["url"])
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(first["url"])})
    assert r.status_code == 400 and "another sign-in attempt" in r.json()["detail"]
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(second["url"])})
    assert r.status_code == 200


def test_start_unconfigured_is_501_with_the_missing_names(client, settings) -> None:
    settings.zoom_client_id = CLIENT_ID
    r = client.post("/v1/zoom/signin/start")
    assert r.status_code == 501
    detail = r.json()["detail"]
    assert "CONVO_ZOOM_CLIENT_SECRET" in detail and "CONVO_ZOOM_REDIRECT_URL" in detail
    assert "CONVO_ZOOM_CLIENT_ID" not in detail
    assert not zoom_signin.pending_file(settings).exists()


# ------------------------------------------------------------------- finish
def test_finish_with_the_redirect_address(zc, zsettings, zoom, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    url = _start(zc)["url"]
    pasted = _redirect(url)
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": pasted})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "signed_in": True, "join_token": "obf"}
    assert _stored(zsettings) == "refresh-1"
    assert stat.S_IMODE(zoom_signin.token_file(zsettings).stat().st_mode) == 0o600
    assert not zoom_signin.pending_file(zsettings).exists()
    # the verifier from the pending file went to Zoom with the code (PKCE)
    challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
    import base64
    import hashlib
    assert base64.urlsafe_b64encode(hashlib.sha256(zoom.verifier.encode()).digest()) \
        .rstrip(b"=").decode() == challenge
    logs = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("good-code", pasted, "refresh-1", "access-1", CLIENT_SECRET,
                   json.loads(zoom_signin.token_file(zsettings).read_text())["refresh_token"]):
        assert secret not in logs
    assert zc.get("/v1/zoom/signin").json()["signed_in"] is True
    # used up: a second finish has nothing waiting
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": pasted})
    assert r.status_code == 400 and "start the sign-in again" in r.json()["detail"]


def test_finish_with_a_bare_code(zc, zsettings) -> None:
    _start(zc)
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": "  good-code\n"})
    assert r.status_code == 200 and r.json()["signed_in"] is True


@pytest.mark.parametrize(("pasted", "words"), [
    (f"{REDIRECT}?code=good-code&state=someone-else", "another sign-in attempt"),
    (f"{REDIRECT}?code=good-code", "no state"),
    (f"{REDIRECT}?error=access_denied&state=x", "Zoom refused the sign-in"),
    ("", "nothing pasted"),
    ("good code", "paste the whole address"),
])
def test_finish_bad_paste_is_400_and_keeps_the_attempt(zc, zsettings, zoom, pasted,
                                                       words) -> None:
    url = _start(zc)["url"]
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": pasted})
    assert r.status_code == 400 and words in r.json()["detail"]
    assert not zoom_signin.signed_in(zsettings) and zoom.calls == []
    # the owner can paste again
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url)})
    assert r.status_code == 200


def test_finish_without_a_pending_attempt(zc, zoom) -> None:
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": "good-code"})
    assert r.status_code == 400
    assert r.json()["detail"] == "no Zoom sign-in is waiting: start the sign-in again"
    assert zoom.calls == []


def test_finish_after_expiry(zc, zsettings, zoom, monkeypatch) -> None:
    url = _start(zc)["url"]
    later = time.time() + zoom_signin.PENDING_SECONDS + 1
    monkeypatch.setattr(zoom_signin.time, "time", lambda: later)
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url)})
    assert r.status_code == 400 and "start the sign-in again" in r.json()["detail"]
    assert "10 minutes" in r.json()["detail"]
    assert not zoom_signin.pending_file(zsettings).exists() and zoom.calls == []


def test_finish_damaged_pending_file(zc, zsettings) -> None:
    _start(zc)
    zoom_signin.pending_file(zsettings).write_text("{not json")
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": "good-code"})
    assert r.status_code == 400 and "start the sign-in again" in r.json()["detail"]
    assert not zoom_signin.pending_file(zsettings).exists()


def test_zoom_refuses_the_code_is_400(zc, zsettings) -> None:
    url = _start(zc)["url"]
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url, "stale-code")})
    assert r.status_code == 400
    assert "Zoom refused the sign-in code" in r.json()["detail"]
    assert not zoom_signin.signed_in(zsettings)
    assert zoom_signin.pending_file(zsettings).exists()


def test_zoom_refuses_the_client_is_400(zc, zsettings) -> None:
    url = _start(zc)["url"]
    zsettings.zoom_client_secret = "wrong"
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url)})
    assert r.status_code == 400 and "CONVO_ZOOM_CLIENT_SECRET" in r.json()["detail"]


def test_zoom_down_is_502(zc, zsettings, zoom) -> None:
    url = _start(zc)["url"]
    zoom.down = True
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url)})
    assert r.status_code == 502 and "could not reach Zoom" in r.json()["detail"]
    assert zoom_signin.pending_file(zsettings).exists()  # try again when Zoom is back


def test_finish_unconfigured_is_501(client, settings) -> None:
    r = client.post("/v1/zoom/signin/finish", json={"pasted": "good-code"})
    assert r.status_code == 501 and "CONVO_ZOOM_CLIENT_ID" in r.json()["detail"]


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"pasted": 5}', b"{}"])
def test_finish_bad_body_is_400(zc, body) -> None:
    r = zc.post("/v1/zoom/signin/finish", content=body,
                headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


def test_finish_body_is_capped(zc, zoom) -> None:
    _start(zc)
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": "x" * 9000})
    assert r.status_code == 413 and zoom.calls == []
    r = zc.post("/v1/zoom/signin/finish", json={"pasted": "x" * 5000})
    assert r.status_code == 400 and "too long" in r.json()["detail"]


# ------------------------------------------------------ expired at join time
@pytest.fixture
def zclient(zc, zsettings, tmp_path, zoom):
    from test_meetings import _bot

    zsettings.meeting_bot_path, _ = _bot(tmp_path, "record")
    zsettings.meeting_bot_runner = "plain"
    zsettings.zoom_sdk_key, zsettings.zoom_sdk_secret = "fictional-key", "fictional-secret"
    assert zc.get("/v1/meetings").status_code == 200  # makes the launcher
    zc.app.state.meetings.zoom_transport = zoom.transport
    return zc


def test_refused_refresh_token_clears_the_sign_in(zclient, zsettings, tmp_path) -> None:
    zoom_signin.save_refresh_token(zsettings, "refresh-revoked")
    assert zclient.get("/v1/zoom/signin").json()["signed_in"] is True
    r, rep = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 503 and r.json()["detail"] == "zoom_signin_needed"
    message = r.json()["message"]
    assert "Odysseus" in message and "Sign in to Zoom" in message
    assert "convo-server zoom-login" in message
    assert not rep.exists()
    status = zclient.get("/v1/zoom/signin").json()
    assert status["signed_in"] is False and status["join_token"] == "none"
    aside = zoom_signin.token_file(zsettings).with_name("refresh_token.expired.json")
    assert json.loads(aside.read_text())["refresh_token"] == "refresh-revoked"
    # signing in again from Odysseus works and the next join gets its token
    url = _start(zclient)["url"]
    r = zclient.post("/v1/zoom/signin/finish", json={"pasted": _redirect(url)})
    assert r.status_code == 200 and r.json()["signed_in"] is True


def test_wrong_client_at_join_keeps_the_sign_in(zclient, zsettings, tmp_path) -> None:
    """A bad client secret is a settings problem, not an expired sign-in."""
    zoom_signin.save_refresh_token(zsettings, "refresh-0")
    zsettings.zoom_client_secret = "wrong"
    r, _ = _join(zclient, tmp_path, zsettings)
    assert r.status_code == 503 and "CONVO_ZOOM_CLIENT_SECRET" in r.json()["message"]
    assert zoom_signin.signed_in(zsettings)


# -------------------------------------------------------------------- relay
class Upstream:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.status, self.body = status, {"ok": True} if body is None else body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        return httpx.Response(self.status, json=self.body)


def fake(pipeline, status: int = 200, body: object | None = None):
    up = Upstream(status, body)
    return mcp_client(pipeline, httpx.MockTransport(up)), up


def test_relay_forwards_with_the_ingest_token(pipeline) -> None:
    status = {"configured": True, "signed_in": False, "join_token": "none", "missing": []}
    c, up = fake(pipeline, 200, status)
    assert c.get(f"/zoom/signin{Q}").json() == status
    c.post(f"/zoom/signin/start{Q}")
    payload = b'{"pasted": "fictional-code"}'
    c.post("/zoom/signin/finish", content=payload,
           headers={"Content-Type": "application/json",
                    "Authorization": f"Bearer {MCP_TOKEN}"})
    got = [(r.method, str(r.url)) for r in up.seen]
    base = "http://127.0.0.1:8000"
    assert got == [("GET", f"{base}/v1/zoom/signin"), ("POST", f"{base}/v1/zoom/signin/start"),
                   ("POST", f"{base}/v1/zoom/signin/finish")]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in up.seen)
    assert up.seen[2].content == payload
    assert not any(MCP_TOKEN in str(r.url) for r in up.seen)


@pytest.mark.parametrize(("status", "body"), [
    (501, {"detail": "Zoom sign-in is not set up: CONVO_ZOOM_CLIENT_ID missing"}),
    (400, {"detail": "no Zoom sign-in is waiting: start the sign-in again"}),
    (502, {"detail": "could not reach Zoom: ConnectError"}),
    (200, {"ok": True, "signed_in": True, "join_token": "obf"}),
])
def test_relay_passes_status_and_json_through(pipeline, status, body) -> None:
    c, _ = fake(pipeline, status, body)
    r = c.post(f"/zoom/signin/finish{Q}", json={"pasted": "fictional-code"})
    assert (r.status_code, r.json()) == (status, body)
    r = c.post(f"/zoom/signin/start{Q}")
    assert (r.status_code, r.json()) == (status, body)


def test_relay_needs_the_mcp_token(pipeline) -> None:
    c, up = fake(pipeline)
    assert c.get("/zoom/signin").status_code == 401
    assert c.post("/zoom/signin/start?access_token=nope").status_code == 401
    r = c.post("/zoom/signin/finish", json={"pasted": "x"},
               headers={"Authorization": f"Bearer {TOKEN}"})  # the ingest token isn't it
    assert r.status_code == 401
    assert not up.seen


def test_relay_finish_needs_json_and_a_small_body(pipeline) -> None:
    c, up = fake(pipeline)
    assert c.post(f"/zoom/signin/finish{Q}", data={"pasted": "x"}).status_code == 415
    r = c.post(f"/zoom/signin/finish{Q}", json={"pasted": "x" * 9000})
    assert r.status_code == 413 and not up.seen


def test_relay_end_to_end(pipeline, zsettings, zoom, monkeypatch, caplog) -> None:
    """Relay → real ingest API (in process) → fake Zoom."""
    caplog.set_level(logging.DEBUG)
    app = create_app(pipeline)
    app.state.zoom_transport = zoom.transport
    with mcp_client(pipeline, httpx.ASGITransport(app=app)) as c:
        assert c.get(f"/zoom/signin{Q}").json()["configured"] is True
        url = c.post(f"/zoom/signin/start{Q}").json()["url"]
        pasted = _redirect(url)
        r = c.post(f"/zoom/signin/finish{Q}", json={"pasted": pasted})
        assert r.status_code == 200 and r.json()["signed_in"] is True
        assert c.get(f"/zoom/signin{Q}").json()["signed_in"] is True
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "good-code" not in logs and "refresh-1" not in logs and pasted not in logs


def test_signin_routes_are_not_mcp_tools(pipeline) -> None:
    import asyncio

    from mcp import Client

    from convo_server.ingest_relay import add_ingest_routes
    from convo_server.mcp_server import build_mcp

    def tool_names(with_routes: bool) -> set[str]:
        mcp = build_mcp(pipeline.store, pipeline.settings)
        if with_routes:
            add_ingest_routes(mcp, pipeline.settings)

        async def names():
            async with Client(mcp) as client:
                return {t.name for t in (await client.list_tools()).tools}

        return asyncio.run(names())

    tools = tool_names(True)
    assert tools == tool_names(False)
    assert not [n for n in tools if "zoom" in n or "signin" in n or "sign_in" in n]


# ---------------------------------------------------------------- idle cost
def test_building_the_apps_imports_no_zoom_code(tmp_path) -> None:
    """Neither the API nor the MCP app imports zoom_signin until a sign-in request (a
    fresh interpreter, so other tests can't pollute sys.modules)."""
    code = textwrap.dedent(f"""
        import sys
        from convo_server.config import Settings
        from convo_server.store import Store
        from convo_server.pipeline import Pipeline
        from convo_server.vad import EnergyVAD
        from convo_server.api import create_app
        from convo_server.mcp_server import BearerAuth, build_mcp
        from convo_server.ingest_relay import add_ingest_routes
        s = Settings(ingest_token="x" * 20, mcp_token="m" * 20, data_dir={str(tmp_path)!r},
                     run_worker=False, vad_backend="energy")
        create_app(Pipeline(s, Store(s.db_path), EnergyVAD()))
        mcp = build_mcp(Store(s.db_path), s)
        add_ingest_routes(mcp, s)
        BearerAuth(mcp.streamable_http_app(), s.mcp_token)
        bad = [m for m in sys.modules if m in ("convo_server.zoom_signin",
                                               "convo_server.meetings")]
        assert not bad, bad
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parents[1], timeout=60)
    assert r.stdout.strip() == "ok", r.stderr


@pytest.mark.parametrize("kwargs", [
    {}, {"json": {}}, {"json": {"from": "odysseus", "extra": [1]}},
    {"content": b"", "headers": {"Content-Type": "application/json"}}])
def test_start_accepts_no_body_or_any_json(zc, pipeline, zsettings, zoom, kwargs) -> None:
    """The hub sends {} with application/json; unknown fields are ignored."""
    r = zc.post("/v1/zoom/signin/start", **kwargs)
    assert r.status_code == 200 and r.json()["expires_in"] == 600
    app = create_app(pipeline)
    with mcp_client(pipeline, httpx.ASGITransport(app=app)) as c:
        r = c.post(f"/zoom/signin/start{Q}", **kwargs)
        assert r.status_code == 200 and r.json()["url"].startswith("https://zoom.us/")
