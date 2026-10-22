"""Upload routes on the MCP HTTP server: auth, forwarding to the ingest API, errors."""

from __future__ import annotations

import logging

import httpx
import pytest
from conftest import TOKEN, silence, wav
from starlette.testclient import TestClient

from convo_server import ingest_relay
from convo_server.api import create_app
from convo_server.ingest_relay import add_ingest_routes, ingest_api_base
from convo_server.mcp_server import BearerAuth, build_mcp

MCP_TOKEN = "mcp-token-abcdefghijkl"
T0 = 1_760_000_000.0


def mcp_client(pipeline, transport) -> TestClient:
    mcp = build_mcp(pipeline.store, pipeline.settings)
    add_ingest_routes(mcp, pipeline.settings, transport=transport)
    return TestClient(BearerAuth(mcp.streamable_http_app(), MCP_TOKEN))


def no_tokens_logged(caplog) -> bool:
    """The module's own log lines (the test client logs its own URLs) carry no token."""
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("convo_server")]
    assert ours, "expected a log line to check"
    return not any(TOKEN in m or MCP_TOKEN in m for m in ours)


@pytest.fixture
def relay(pipeline):
    """MCP HTTP app whose upload routes reach the real ingest API in-process."""
    return mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline)))


def upload(c, seq=0, *, session="call-1", data=None, query="", headers=None, **fields):
    form = {"device": "browser", "session_id": session, "sequence": str(seq),
            "timestamp": str(T0 + 30 * seq), "codec": "wav", **fields}
    return c.post(f"/ingest/chunk{query}", data=form, headers=headers,
                  files={"audio": ("c.wav", data or wav(silence(1)), "audio/wav")})


def test_upload_needs_the_mcp_token(relay):
    assert upload(relay).status_code == 401
    assert upload(relay, query="?access_token=nope").status_code == 401
    # The ingest token is not the MCP token: it is never accepted here.
    assert upload(relay, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    assert upload(relay, headers={"Authorization": f"Bearer {MCP_TOKEN}"}).status_code == 200


def test_upload_and_end_reach_the_store(relay, pipeline):
    r = upload(relay, 0, query=f"?access_token={MCP_TOKEN}", kind="note")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["session_id"] == "call-1" and body["sequence"] == 0
    assert upload(relay, 1, query=f"?access_token={MCP_TOKEN}", kind="note").json()["ok"]
    # A retry stays idempotent and the API's rules apply: same code path.
    again = upload(relay, 1, query=f"?access_token={MCP_TOKEN}", kind="note")
    assert again.json()["duplicate"] is True
    mixed = upload(relay, 2, query=f"?access_token={MCP_TOKEN}")
    assert mixed.status_code == 400 and "note session" in mixed.json()["detail"]
    session = pipeline.store.get_session("call-1")
    assert session["device"] == "browser" and session["kind"] == "note"

    r = relay.post(f"/sessions/call-1/end?access_token={MCP_TOKEN}")
    assert r.status_code == 200 and r.json() == {"ok": True, "stop": True}


def test_api_status_and_detail_pass_through(relay):
    q = f"?access_token={MCP_TOKEN}"
    r = upload(relay, query=q, session="../etc")
    assert r.status_code == 400 and "detail" in r.json()
    r = upload(relay, query=q, timestamp="yesterday")
    assert r.status_code == 400 and "timestamp" in r.json()["detail"]
    r = relay.post(f"/sessions/nope/end{q}")
    assert r.status_code == 404 and r.json() == {"detail": "session not found"}


def test_needs_multipart(relay):
    r = relay.post(f"/ingest/chunk?access_token={MCP_TOKEN}", json={"audio": "x"})
    assert r.status_code == 415


def test_body_size_is_capped(pipeline, monkeypatch):
    seen = []
    c = mcp_client(pipeline, httpx.MockTransport(lambda r: seen.append(r) or
                                                 httpx.Response(200, json={"ok": True})))
    monkeypatch.setattr(ingest_relay, "MAX_BODY_BYTES", 4096)
    r = upload(c, query=f"?access_token={MCP_TOKEN}", data=b"x" * 8192)
    assert r.status_code == 413 and r.json() == {"detail": "chunk too large"}
    assert not seen  # never forwarded


def test_forwards_with_ingest_token_and_keeps_tokens_out_of_logs(pipeline, caplog):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "stop": False})

    c = mcp_client(pipeline, httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        assert upload(c, query=f"?access_token={MCP_TOKEN}").json() == {"ok": True,
                                                                          "stop": False}
    (req,) = seen
    assert str(req.url) == "http://127.0.0.1:8000/v1/ingest/chunk"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert MCP_TOKEN not in str(req.url)
    assert no_tokens_logged(caplog)


def test_api_down_is_a_readable_502(pipeline, caplog):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = mcp_client(pipeline, httpx.MockTransport(down))
    with caplog.at_level(logging.DEBUG):
        r = upload(c, query=f"?access_token={MCP_TOKEN}")
        e = c.post(f"/sessions/s1/end?access_token={MCP_TOKEN}")
    assert r.status_code == 502 and e.status_code == 502
    assert "not reachable" in r.json()["detail"]
    assert no_tokens_logged(caplog)


def test_non_json_answer_becomes_detail(pipeline):
    c = mcp_client(pipeline, httpx.MockTransport(
        lambda r: httpx.Response(503, text="Service Unavailable")))
    r = upload(c, query=f"?access_token={MCP_TOKEN}")
    assert r.status_code == 503 and r.json() == {"detail": "Service Unavailable"}


def test_routes_are_not_mcp_tools(pipeline):
    import asyncio

    from mcp import Client

    mcp = build_mcp(pipeline.store, pipeline.settings)
    add_ingest_routes(mcp, pipeline.settings)

    async def names():
        async with Client(mcp) as client:
            return {t.name for t in (await client.list_tools()).tools}

    tools = asyncio.run(names())
    assert not {n for n in tools if "ingest" in n or "upload" in n or "end" in n}


@pytest.mark.parametrize(("host", "base"), [
    ("127.0.0.1", "http://127.0.0.1:8000"),
    ("0.0.0.0", "http://127.0.0.1:8000"),
    ("100.64.0.10", "http://100.64.0.10:8000"),
    ("::", "http://127.0.0.1:8000"),
    ("::1", "http://[::1]:8000"),
])
def test_ingest_api_base(settings, host, base):
    assert ingest_api_base(settings.model_copy(update={"host": host})) == base


def test_access_log_hides_the_mcp_token():
    from convo_server.mcp_server import HideAccessToken, _log_config

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "POST",
                               f"/mcp?access_token={MCP_TOKEN}&x=1", "1.1", 200), None)
    assert HideAccessToken().filter(record)
    assert MCP_TOKEN not in record.getMessage()
    assert "access_token=***&x=1" in record.getMessage()
    assert _log_config()["handlers"]["access"]["filters"] == ["hide_access_token"]


def test_read_routes_reach_the_api(relay, pipeline):
    q = f"?access_token={MCP_TOKEN}"
    assert relay.get(f"/health{q}").json() == {"ok": True}
    assert relay.get("/health").status_code == 401  # behind the MCP token, unlike the API's
    convs = relay.get(f"/conversations{q}&limit=5")
    assert convs.status_code == 200 and convs.json()["conversations"] == []
    assert relay.get(f"/action-items{q}&status=open").json() == {"action_items": []}
    r = relay.get(f"/conversations/nope{q}")
    assert r.status_code == 404 and r.json() == {"detail": "conversation not found"}
    assert relay.get(f"/conversations/nope/transcript{q}").status_code == 404
    assert relay.get(f"/digests/2025-10-09{q}").status_code == 404
    r = relay.patch(f"/action-items/99{q}", json={"status": "done"})
    assert r.status_code == 404 and r.json() == {"detail": "action item not found"}
    assert relay.post(f"/digests/not-a-date{q}").status_code == 400


def test_read_routes_drop_the_mcp_token_from_the_query(pipeline):
    seen: list[httpx.Request] = []
    c = mcp_client(pipeline, httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(200, json={"conversations": []})))
    c.get(f"/conversations?since=5&access_token={MCP_TOKEN}&limit=2")
    (req,) = seen
    assert req.url.path == "/v1/conversations"
    assert dict(req.url.params) == {"since": "5", "limit": "2"}
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


def test_only_listed_routes_exist(relay):
    q = f"?access_token={MCP_TOKEN}"
    for path in ("/sessions", "/search", "/speakers", "/v1/conversations"):
        assert relay.get(path + q).status_code in (404, 405), path
    assert relay.delete(f"/action-items/1{q}").status_code == 405
