"""Every relay route on the MCP HTTP app, pinned against a fake ingest API.

For each case the test records what the relay sends to the ingest API (method, URL,
headers with the ingest token redacted, body, timeout) and what it answers (status,
content type, body), and compares the whole record with ``relay_routes.golden.json``.
Cases cover the normal answer, the API's errors (404, 409 with ``message`` and
``active``, 422, a non-JSON 500), an API that times out or can't be reached, bad ids,
wrong content types, bodies over the cap, and a request without the MCP token.

Regenerate (only when a change to the relay is intended) with
``UPDATE_SNAPSHOTS=1 pytest tests/unit/mcp/test_relay_characterization.py``.
Fictional data only.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from tests.conftest import TOKEN
from tests.unit.mcp.test_ingest_relay import MCP_TOKEN, mcp_client

GOLDEN = Path(__file__).parent / "relay_routes.golden.json"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
MID = "0123456789abcdef0123456789abcdef"
BOUNDARY = "fictional-boundary-0123"
CHUNK = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"device\"\r\n\r\n"
         f"browser\r\n--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"audio\"; "
         f"filename=\"c.wav\"\r\nContent-Type: audio/wav\r\n\r\nRIFF0000\r\n"
         f"--{BOUNDARY}--\r\n").encode()
MULTIPART = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
JSON = {"Content-Type": "application/json"}
# A query every route gets: the MCP token (never forwarded) and a few others.
QUERY = {"access_token": MCP_TOKEN, "limit": "7", "format": "md", "include": "summary",
         "other": "x y"}

# (name, method, path on the relay, request kwargs) for every relay route.
ROUTES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("ingest_chunk", "POST", "/ingest/chunk", {"content": CHUNK, "headers": MULTIPART}),
    ("session_end", "POST", "/sessions/call 1/end", {}),
    ("ingest_text", "POST", "/ingest/text",
     {"content": b'{"source_id": "hub-note:1", "text": "Fictional"}', "headers": JSON}),
    ("ingest_text_delete", "DELETE", "/ingest/text/hub-note:1 x", {}),
    ("ingest_messages", "POST", "/ingest/messages",
     {"content": b'{"source": "imessage", "messages": []}', "headers": JSON}),
    ("ingest_messages_chat_delete", "DELETE",
     "/ingest/messages/chats/imessage/iMessage;-;+15550100", {}),
    ("health", "GET", "/health", {}),
    ("conversations", "GET", "/conversations", {}),
    ("conversation", "GET", "/conversations/conv 1", {}),
    ("transcript", "GET", "/conversations/conv 1/transcript", {}),
    ("action_items", "GET", "/action-items", {}),
    ("action_item_patch", "PATCH", "/action-items/5",
     {"content": b'{"status": "done"}', "headers": {"Content-Type": "text/plain"}}),
    ("digest", "GET", "/digests/2026-10-14", {}),
    ("digest_make", "POST", "/digests/2026-10-14", {}),
    ("settings_llm", "GET", "/settings/llm", {}),
    ("settings_llm_put", "PUT", "/settings/llm",
     {"content": b'{"model": "fictional-7b"}', "headers": JSON}),
    ("settings_vocabulary", "GET", "/settings/vocabulary", {}),
    ("settings_vocabulary_put", "PUT", "/settings/vocabulary",
     {"content": b'{"words": "Lisbon, Jonas"}', "headers": JSON}),
    ("enroll_next", "GET", "/speakers/enroll/next", {}),
    ("enroll_next_start", "POST", "/speakers/enroll/next",
     {"content": b'{"name": "Alice"}', "headers": JSON}),
    ("enroll_next_cancel", "DELETE", "/speakers/enroll/next", {}),
    ("export", "GET", "/conversations/conv 1/export", {}),
    ("meeting_join", "POST", "/meetings",
     {"content": b'{"url": "https://zoom.us/j/123456789", "title": "Fictional"}',
      "headers": JSON}),
    ("meetings", "GET", "/meetings", {}),
    ("meeting", "GET", f"/meetings/{MID}", {}),
    ("meeting_leave", "DELETE", f"/meetings/{MID}", {}),
    ("zoom_signin", "GET", "/zoom/signin", {}),
    ("zoom_signin_start", "POST", "/zoom/signin/start", {}),
    ("zoom_signin_finish", "POST", "/zoom/signin/finish",
     {"content": b'{"redirect": "https://example.invalid/cb?code=fictional"}',
      "headers": JSON}),
]

# What the fake ingest API answers: (status, JSON body | text | exception class).
ANSWERS: dict[str, Any] = {
    "ok": (200, {"ok": True, "items": [1, 2]}),
    "accepted": (202, {"id": MID, "state": "starting"}),
    "not_found": (404, {"detail": "not found"}),
    "conflict": (409, {"detail": "a bot is already in a meeting",
                       "message": "Athena is already in a meeting",
                       "active": {"id": MID, "state": "in_meeting"}}),
    "conflict_no_detail": (409, {"message": "Athena is already in a meeting",
                                 "active": {"id": MID}}),
    "invalid": (422, {"detail": [{"loc": ["body", "url"], "msg": "field required"}]}),
    "error_list": (400, ["bad", "worse"]),
    "server_error": (500, "upstream broke"),
    "empty_error": (503, ""),
    "timeout": httpx.ReadTimeout,
    "unreachable": httpx.ConnectError,
}


class FakeAPI:
    """A MockTransport standing in for the ingest API; records every request."""

    def __init__(self) -> None:
        self.answer = "ok"
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        headers = {k: v.replace(TOKEN, "<INGEST_TOKEN>") for k, v in request.headers.items()
                   if k != "user-agent"}
        assert MCP_TOKEN not in str(request.url) and MCP_TOKEN not in str(request.headers)
        self.seen.append({
            "method": request.method,
            "url": str(request.url),
            "headers": headers,
            "body": body.decode("utf-8", "replace") if len(body) < 4096
            else f"<{len(body)} bytes>",
            "timeout": request.extensions.get("timeout"),
        })
        if self.answer == "file":
            return httpx.Response(200, content=b"# Fictional\n", headers={
                "content-type": "text/markdown; charset=utf-8",
                "content-disposition": 'attachment; filename="fictional.md"',
                "cache-control": "no-store", "x-content-type-options": "nosniff",
                "x-other": "dropped"})
        spec = ANSWERS[self.answer]
        if isinstance(spec, type):
            raise spec("fictional failure", request=request)
        status, body_out = spec
        if isinstance(body_out, str):
            return httpx.Response(status, text=body_out)
        return httpx.Response(status, json=body_out)


def _response(resp: httpx.Response) -> dict[str, Any]:
    out: dict[str, Any] = {"status": resp.status_code,
                           "content_type": resp.headers.get("content-type")}
    try:
        out["json"] = resp.json()
    except ValueError:
        out["text"] = resp.text
    for k in ("content-disposition", "cache-control", "x-content-type-options", "x-other"):
        if k in resp.headers:
            out[k] = resp.headers[k]
    return out


def _cases() -> list[tuple[str, str, str, str, dict[str, Any], str | None]]:
    """(case, answer, method, path, kwargs, query) for the whole table."""
    cases = []
    for name, method, path, kwargs in ROUTES:
        for answer in ANSWERS:
            cases.append((f"{name}__{answer}", answer, method, path, kwargs, "query"))
        cases.append((f"{name}__no_query", "ok", method, path, kwargs, None))
        cases.append((f"{name}__no_token", "ok", method, path, kwargs, "no_token"))
    cases.append(("export__file", "file", "GET", "/conversations/conv 1/export", {},
                  "query"))
    # Inputs the relay refuses or rewrites itself.
    extra: list[tuple[str, str, str, dict[str, Any]]] = [
        ("meeting__bad_id", "GET", "/meetings/not-a-meeting", {}),
        ("meeting__upper_id", "GET", f"/meetings/{MID.upper()}", {}),
        ("meeting__short_id", "GET", f"/meetings/{MID[:-1]}", {}),
        ("meeting_leave__bad_id", "DELETE", "/meetings/zz", {}),
        ("meeting_join__not_json", "POST", "/meetings",
         {"content": b"url=x", "headers": {"Content-Type": "text/plain"}}),
        ("meeting_join__no_type", "POST", "/meetings", {"content": b"{}"}),
        ("meeting_join__too_large", "POST", "/meetings",
         {"content": b"{" + b" " * (16 * 1024) + b"}", "headers": JSON}),
        ("meeting_join__json_charset", "POST", "/meetings",
         {"content": b"{}", "headers": {"Content-Type": "Application/JSON; charset=utf-8"}}),
        ("zoom_signin_finish__not_json", "POST", "/zoom/signin/finish",
         {"content": b"x", "headers": {"Content-Type": "text/plain"}}),
        ("zoom_signin_finish__too_large", "POST", "/zoom/signin/finish",
         {"content": b"{" + b" " * (8 * 1024) + b"}", "headers": JSON}),
        ("ingest_text__not_json", "POST", "/ingest/text",
         {"content": b"x", "headers": {"Content-Type": "text/plain"}}),
        ("ingest_messages__not_json", "POST", "/ingest/messages",
         {"content": b"x", "headers": {"Content-Type": "text/plain"}}),
        ("ingest_text__too_large", "POST", "/ingest/text",
         {"content": b"{" + b" " * (200_000 * 6 + 16 * 1024) + b"}", "headers": JSON}),
        ("ingest_chunk__not_multipart", "POST", "/ingest/chunk",
         {"content": b"{}", "headers": JSON}),
        ("settings_llm_put__too_large", "PUT", "/settings/llm",
         {"content": b"{" + b" " * (64 * 1024) + b"}", "headers": JSON}),
        ("settings_llm_put__exact_cap", "PUT", "/settings/llm",
         {"content": b"{" + b" " * (64 * 1024 - 2) + b"}", "headers": JSON}),
        ("unknown_route", "GET", "/meetings/x/y", {}),
        ("wrong_method", "PUT", "/meetings", {}),
        ("wrong_method_zoom", "GET", "/zoom/signin/start", {}),
        ("wrong_method_finish", "GET", "/zoom/signin/finish", {}),
    ]
    for name, method, path, kwargs in extra:
        cases.append((name, "ok", method, path, kwargs, "query"))
    for raw in ("0", "1", "100", "101", "1000", "0007", "9" * 7, "", " 5 ", "abc", "-1",
                "²", "1.5"):
        cases.append((f"meetings__limit_{raw!r}", "ok", "GET", "/meetings",
                      {"params": {"limit": raw, "access_token": MCP_TOKEN, "x": "1"}}, None))
    cases.append(("conversations__multi_query", "ok", "GET", "/conversations",
                  {"params": [("person", "Alice"), ("person", "Jonas"),
                              ("access_token", MCP_TOKEN)]}, None))
    cases.append(("export__multi_query", "ok", "GET", "/conversations/c/export",
                  {"params": [("include", "summary"), ("include", "facts"),
                              ("access_token", MCP_TOKEN), ("format", "html")]}, None))
    return cases


def _run(client: TestClient, api: FakeAPI, case: tuple) -> dict[str, Any]:
    _, answer, method, path, kwargs, query = case
    api.answer, api.seen = answer, []
    kwargs = dict(kwargs)
    headers = dict(kwargs.pop("headers", {}))
    if query == "no_token":
        pass
    elif query == "query":
        kwargs["params"] = QUERY
    elif "params" not in kwargs:
        headers["Authorization"] = f"Bearer {MCP_TOKEN}"
    resp = client.request(method, path, headers=headers, **kwargs)
    return {"request": {"method": method, "path": path, "query": query},
            "forwarded": api.seen, "response": _response(resp)}


def test_every_relay_route_is_pinned(pipeline) -> None:
    api = FakeAPI()
    client = mcp_client(pipeline, httpx.MockTransport(api))
    got = {case[0]: _run(client, api, case) for case in _cases()}
    text = json.dumps(got, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    if UPDATE:
        GOLDEN.write_text(text, encoding="utf-8")
        return
    want = GOLDEN.read_text(encoding="utf-8")
    if want != text:
        diff = "".join(difflib.unified_diff(
            want.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=GOLDEN.name, tofile="now", n=8))
        pytest.fail(f"relay behaviour changed:\n{diff[:20000]}")


def test_the_table_covers_every_relay_route(pipeline) -> None:
    """A route added to the relay without a case here fails."""
    from starlette.routing import Route

    from athena.ingest_relay import add_ingest_routes
    from athena.mcp_server import build_mcp

    mcp = build_mcp(pipeline.store, pipeline.settings)
    before = {(m, r.path) for r in mcp.streamable_http_app().routes
              if isinstance(r, Route) for m in (r.methods or ())}
    add_ingest_routes(mcp, pipeline.settings, transport=httpx.MockTransport(FakeAPI()))
    after = {(m, r.path) for r in mcp.streamable_http_app().routes
             if isinstance(r, Route) for m in (r.methods or ()) if m != "HEAD"}
    relay = {(m, p) for m, p in after - before}
    covered = set()
    for _, method, path, _ in ROUTES:
        for m, p in relay:
            pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "$"
            if m == method and re.match(pattern, path):
                covered.add((m, p))
    assert covered == relay, sorted(relay - covered)
