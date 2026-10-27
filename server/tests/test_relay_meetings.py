"""Meeting routes on the MCP HTTP server (lane 4A): forwarded to /v1/meetings, never tools.

Fictional data only; the end-to-end tests start tests/fake_meeting_bot.py, which never
joins anything.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
from conftest import TOKEN
from starlette.testclient import TestClient
from test_ingest_relay import MCP_TOKEN, mcp_client
from test_meetings import SDK_KEY, SDK_SECRET, _bot, _gone

from convo_server import ingest_relay
from convo_server.api import create_app
from convo_server.ingest_relay import add_ingest_routes
from convo_server.mcp_server import build_mcp

Q = f"?access_token={MCP_TOKEN}"
MID = "0123456789abcdef0123456789abcdef"


class Upstream:
    """A MockTransport that records requests and answers with a canned response."""

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


# ------------------------------------------------------------------ forwarding
def test_join_forwards_body_and_ingest_token(pipeline):
    c, up = fake(pipeline, 202, {"id": MID, "state": "starting", "session_id": f"zoom-{MID}"})
    payload = b'{"url": "https://zoom.us/j/123456789?pwd=Fict1", "title": "Fictional sync"}'
    r = c.post(f"/meetings{Q}", content=payload,
               headers={"Content-Type": "application/json"})
    assert r.status_code == 202 and r.json()["id"] == MID
    (req,) = up.seen
    assert req.method == "POST" and str(req.url) == "http://127.0.0.1:8000/v1/meetings"
    assert req.content == payload
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["content-type"] == "application/json"
    assert MCP_TOKEN not in str(req.url)


def test_list_get_and_leave_forward(pipeline):
    c, up = fake(pipeline, 200, {"meetings": []})
    assert c.get(f"/meetings{Q}&limit=5").json() == {"meetings": []}
    c.get(f"/meetings{Q}")
    c.get(f"/meetings/{MID}{Q}")
    c.delete(f"/meetings/{MID}", headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    got = [(r.method, r.url.path, dict(r.url.params)) for r in up.seen]
    assert got == [("GET", "/v1/meetings", {"limit": "5"}), ("GET", "/v1/meetings", {}),
                   ("GET", f"/v1/meetings/{MID}", {}), ("DELETE", f"/v1/meetings/{MID}", {})]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in up.seen)


@pytest.mark.parametrize(("raw", "sent"), [("0", "1"), ("1000", "100"), ("20", "20")])
def test_list_limit_is_bounded(pipeline, raw, sent):
    c, up = fake(pipeline, 200, {"meetings": []})
    c.get(f"/meetings{Q}&limit={raw}&other=x")
    assert dict(up.seen[0].url.params) == {"limit": sent}


def test_list_limit_must_be_a_number(pipeline):
    c, up = fake(pipeline)
    r = c.get(f"/meetings{Q}&limit=lots")
    assert r.status_code == 400 and "limit" in r.json()["detail"] and not up.seen


@pytest.mark.parametrize(("status", "body"), [
    (409, {"detail": "busy", "message": "Athena is already in a meeting (1 of 1 bot).",
           "active": MID}),
    (501, {"detail": "meetings are off: set CONVO_MEETING_BOT_PATH"}),
    (400, {"detail": "not a Zoom meeting link"}),
    (500, {"detail": "could not start the meeting bot: fictional"}),
])
def test_join_errors_pass_through_with_their_fields(pipeline, status, body):
    c, _ = fake(pipeline, status, body)
    r = c.post(f"/meetings{Q}", json={"meeting_id": "123456789"})
    assert r.status_code == status and r.json() == body


PYDANTIC_422 = {"detail": [
    {"type": "missing", "loc": ["body", "url"], "msg": "Field required", "input": {}},
    {"type": "string_too_long", "loc": ["body", "title"],
     "msg": "String should have at most 200 characters", "input": "x"},
]}


def test_error_list_detail_becomes_a_readable_string(pipeline):
    """QA BUG-23: FastAPI's 422 list ``detail`` reaches the hub as one string."""
    c, _ = fake(pipeline, 422, PYDANTIC_422)
    r = c.post(f"/meetings{Q}", json={"title": "x"})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)
    assert body["detail"] == ("body.url: Field required; "
                              "body.title: String should have at most 200 characters")
    assert body["errors"] == PYDANTIC_422["detail"]


@pytest.mark.parametrize("raw", [["boom", "bang"], "plain words", 42, None,
                                 {"detail": {"why": "nested"}}, {"message": "no detail"}])
def test_error_json_without_a_string_detail_gets_one(pipeline, raw):
    """QA BUG-23: a non-object JSON error, or an object without a string detail."""
    c, _ = fake(pipeline, 500, raw)
    for r in (c.post(f"/meetings{Q}", json={"meeting_id": "123456789"}),
              c.get(f"/meetings/{MID}{Q}"), c.delete(f"/meetings/{MID}{Q}")):
        assert r.status_code == 500
        assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_successful_non_object_answers_are_untouched(pipeline):
    c, _ = fake(pipeline, 200, ["a", "b"])
    r = c.get(f"/meetings{Q}")
    assert r.status_code == 200 and r.json() == ["a", "b"]


def test_unknown_meeting_passes_through(pipeline):
    c, _ = fake(pipeline, 404, {"detail": "meeting not found"})
    for method in ("GET", "DELETE"):
        r = c.request(method, f"/meetings/{'f' * 32}{Q}")
        assert r.status_code == 404 and r.json() == {"detail": "meeting not found"}


@pytest.mark.parametrize("bad", ["%2e%2e", "ABCDEF0123456789abcdef0123456789", "123",
                                 MID + "0", "zoom-" + MID, "%00"])
def test_bad_meeting_id_never_reaches_the_api(pipeline, bad):
    c, up = fake(pipeline)
    for method in ("GET", "DELETE"):
        r = c.request(method, f"/meetings/{bad}{Q}")
        assert r.status_code == 404 and r.json() == {"detail": "meeting not found"}, bad
    assert not up.seen


@pytest.mark.parametrize("bad", ["..", "../sessions", "../../v1/meetings", MID + "%2f..",
                                 "..%2f..%2fsessions"])
def test_dot_segments_and_slashes_never_reach_the_api(pipeline, bad):
    c, up = fake(pipeline)
    for method in ("GET", "DELETE"):
        assert c.request(method, f"/meetings/{bad}{Q}").status_code in (404, 405)
    assert not up.seen


def test_join_needs_json_and_a_small_body(pipeline):
    c, up = fake(pipeline)
    assert c.post(f"/meetings{Q}", data={"url": "x"}).status_code == 415
    r = c.post(f"/meetings{Q}", json={"url": "https://zoom.us/j/123456789", "title": "x" * 20000})
    assert r.status_code == 413 and r.json() == {"detail": "request too large"}
    assert not up.seen


def test_meeting_routes_need_the_mcp_token(pipeline):
    c, up = fake(pipeline)
    assert c.post("/meetings", json={"meeting_id": "123456789"}).status_code == 401
    assert c.get("/meetings").status_code == 401
    assert c.get(f"/meetings/{MID}?access_token=nope").status_code == 401
    # the ingest token is not accepted here
    r = c.delete(f"/meetings/{MID}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401
    assert not up.seen


def test_api_down_is_a_readable_502(pipeline):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = mcp_client(pipeline, httpx.MockTransport(down))
    for r in (c.post(f"/meetings{Q}", json={"meeting_id": "123456789"}),
              c.get(f"/meetings{Q}"), c.get(f"/meetings/{MID}{Q}"),
              c.delete(f"/meetings/{MID}{Q}")):
        assert r.status_code == 502 and "not reachable" in r.json()["detail"]


def test_leave_gets_a_longer_timeout_than_the_other_meeting_routes(pipeline):
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.method] = request.extensions["timeout"]
        return httpx.Response(200, json={})

    c = mcp_client(pipeline, httpx.MockTransport(handler))
    c.delete(f"/meetings/{MID}{Q}")
    c.get(f"/meetings/{MID}{Q}")
    # DELETE waits for SIGTERM, a 10 s grace, SIGKILL and up to 5 s for the last state
    assert seen["DELETE"]["read"] == 30.0 and seen["DELETE"]["connect"] == 5.0
    assert seen["GET"]["read"] == 15.0
    assert ingest_relay.TIMEOUT.read == 120.0  # uploads keep theirs


def test_leave_timeout_is_a_502(pipeline):
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    c = mcp_client(pipeline, httpx.MockTransport(slow))
    r = c.delete(f"/meetings/{MID}{Q}")
    assert r.status_code == 502 and "not reachable" in r.json()["detail"]


# ------------------------------------------------------------- not MCP tools
def test_meeting_routes_are_not_mcp_tools(pipeline):
    import asyncio

    from mcp import Client

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
    assert not [n for n in tools if "meeting" in n or "zoom" in n or "join" in n]


# --------------------------------------------------------------- idle cost
def test_building_the_mcp_app_skips_meeting_code(tmp_path):
    """Building the MCP HTTP app with the relay routes imports no meeting code and starts
    no thread (a fresh interpreter, so other tests can't pollute sys.modules)."""
    code = textwrap.dedent(f"""
        import sys, threading
        from convo_server.config import Settings
        from convo_server.store import Store
        from convo_server.mcp_server import BearerAuth, build_mcp
        from convo_server.ingest_relay import add_ingest_routes
        s = Settings(ingest_token="x" * 20, mcp_token="m" * 20, data_dir={str(tmp_path)!r},
                     run_worker=False, vad_backend="energy", meeting_bot_path="/bin/true")
        mcp = build_mcp(Store(s.db_path), s)
        add_ingest_routes(mcp, s)
        app = BearerAuth(mcp.streamable_http_app(), s.mcp_token)
        assert "convo_server.meetings" not in sys.modules, "meetings imported"
        assert threading.active_count() == 1, threading.enumerate()
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parents[1], timeout=60)
    assert r.stdout.strip() == "ok", r.stderr


# ------------------------------------------------- end to end with a fake bot
@pytest.fixture
def bot_relay(pipeline, tmp_path, monkeypatch):
    """Relay → real ingest API (in process) → launcher → tests/fake_meeting_bot.py."""
    s = pipeline.settings
    s.meeting_bot_path, _ = _bot(tmp_path, "record")
    s.meeting_bot_runner = "plain"
    s.zoom_sdk_key, s.zoom_sdk_secret = SDK_KEY, SDK_SECRET
    monkeypatch.setenv("CONVO_INGEST_TOKEN", TOKEN)
    with mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline))) as c:
        yield c


def _wait(c: TestClient, mid: str, state: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while (row := c.get(f"/meetings/{mid}{Q}").json())["state"] != state:
        assert time.monotonic() < deadline, row
        time.sleep(0.05)
    return row


def test_join_busy_leave_through_the_relay(bot_relay, pipeline):
    c = bot_relay
    r = c.post(f"/meetings{Q}", json={"url": "https://zoom.us/j/123456789?pwd=Fict1",
                                      "title": "Fictional planning"})
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    assert _wait(c, mid, "recording")["title"] == "Fictional planning"
    pid = pipeline.store.get_meeting(mid)["pid"]

    busy = c.post(f"/meetings{Q}", json={"meeting_id": "987654321"})
    assert busy.status_code == 409
    assert busy.json()["detail"] == "busy" and busy.json()["active"] == mid
    assert "message" in busy.json()
    assert [m["id"] for m in c.get(f"/meetings{Q}&limit=5").json()["meetings"]] == [mid]
    bad = c.post(f"/meetings{Q}", json={"url": "https://meet.google.com/abc-defg-hij"})
    assert bad.status_code == 400 and "Zoom" in bad.json()["detail"]

    left = c.delete(f"/meetings/{mid}{Q}")
    assert left.status_code == 200 and left.json()["state"] == "ended"
    assert _gone(pid)  # nothing left running after the meeting
    again = c.delete(f"/meetings/{mid}{Q}")  # idempotent
    assert again.status_code == 200 and again.json()["state"] == "ended"
    assert c.get(f"/meetings/{'e' * 32}{Q}").status_code == 404


def test_meetings_off_is_a_501_through_the_relay(pipeline):
    pipeline.settings.meeting_bot_path = None
    with mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline))) as c:
        r = c.post(f"/meetings{Q}", json={"meeting_id": "123456789"})
    assert r.status_code == 501 and "detail" in r.json()
