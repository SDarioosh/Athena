"""Upload routes on the MCP HTTP server, forwarded to the ingest API.

Odysseus (the hub) reaches this module only through the MCP server and its token. To let
it add recordings (the browser call recorder, phones paired through Odysseus) without a
second URL and token, the MCP HTTP app gets two plain HTTP routes:

    POST /ingest/chunk               same multipart fields and answer as /v1/ingest/chunk
    POST /sessions/{session_id}/end  same as /v1/sessions/{id}/end
    POST /ingest/text                same JSON and answer as /v1/ingest/text (written notes)
    DELETE /ingest/text/{source_id}  same as /v1/ingest/text/{source_id}

The phone relay's T5 sync, paired through Odysseus, also reads a few API routes the same
way (:data:`READ_ROUTES`): the same paths without ``/v1``, same query, same JSON. The
hub's settings page reads and sets the summary model through ``/settings/llm`` and
whisper's vocabulary through ``/settings/vocabulary``.

The hub's Share button downloads a conversation as a file the same way:

    GET /conversations/{id}/export   same query and file as /v1/conversations/{id}/export

The hub's "Send Athena to a meeting" box starts and stops the Zoom bot the same way:

    POST   /meetings                 same JSON and answer as /v1/meetings (join)
    GET    /meetings                 same as /v1/meetings (?limit=1..100)
    GET    /meetings/{id}            same as /v1/meetings/{id} (state)
    DELETE /meetings/{id}            same as /v1/meetings/{id} (leave; waits for the bot)

The hub's "Sign in to Zoom" button signs Athena in to Zoom the same way:

    GET  /zoom/signin                same JSON as /v1/zoom/signin (status)
    POST /zoom/signin/start          same as /v1/zoom/signin/start (authorize URL)
    POST /zoom/signin/finish         same JSON and answer as /v1/zoom/signin/finish

The relay only forwards HTTP: it never imports ``convo_server.meetings`` or
``convo_server.zoom_signin``, so building the MCP app starts and loads nothing
meeting-related.

They are not MCP tools (the model never sees them) and sit behind the same ``BearerAuth``
as ``/mcp``; the MCP token can already read all of this through the tools. Each request is
forwarded unchanged to this module's own ingest API with ``CONVO_INGEST_TOKEN``, so there
is one ingest code path and the ingest token (which also signs transcript links) never
leaves the module.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .api import MAX_CHUNK_BYTES
from .config import Settings
from .pipeline import MAX_TEXT_CHARS

log = logging.getLogger(__name__)

# Room for the multipart form fields and boundaries around the audio.
MAX_BODY_BYTES = MAX_CHUNK_BYTES + 256 * 1024
# A 30 s chunk is decoded and run through VAD before the API answers.
TIMEOUT = httpx.Timeout(120.0, connect=5.0)
_ANY_ADDRESS = {"", "0.0.0.0", "::", "[::]"}
MAX_JSON_BYTES = 64 * 1024
# A written note: its text (up to 4 bytes per character, escaped in JSON) and fields.
MAX_TEXT_BODY_BYTES = MAX_TEXT_CHARS * 6 + 16 * 1024

# Meetings: a join request is a link, a passcode and a title; ids are the launcher's
# uuid4 hex. GET and POST answer quickly; DELETE waits for the bot to exit (SIGTERM, then
# SIGKILL after 10 s, then up to 5 s for its last state), so it gets more time.
MAX_MEETING_BODY_BYTES = 16 * 1024
MEETING_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MEETING_LEAVE_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MEETING_LIST_MAX = 100
_MEETING_ID = re.compile(r"^[0-9a-f]{32}$")

# Zoom sign-in: the finish body is the pasted redirect address (the API caps it at 8 KB);
# finishing makes one call to Zoom (5 s timeout) before the API answers.
MAX_SIGNIN_BODY_BYTES = 8 * 1024

# (method, path on the MCP app); the API path is "/v1" + path ("/health" stays as it is).
READ_ROUTES = [
    ("GET", "/health"),
    ("GET", "/conversations"),
    ("GET", "/conversations/{conv_id}"),
    ("GET", "/conversations/{conv_id}/transcript"),
    ("GET", "/action-items"),
    ("PATCH", "/action-items/{item_id}"),
    ("GET", "/digests/{date}"),
    ("POST", "/digests/{date}"),
    # the hub's settings page: which model writes summaries
    ("GET", "/settings/llm"),
    ("PUT", "/settings/llm"),
    # the hub's settings page: words whisper is nudged toward
    ("GET", "/settings/vocabulary"),
    ("PUT", "/settings/vocabulary"),
]


def ingest_api_base(settings: Settings) -> str:
    """The module's own ingest API, as this process can reach it."""
    host = settings.host.strip()
    if host in _ANY_ADDRESS:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def _json(status: int, detail: str) -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"detail": detail}, status_code=status)


def _readable(value: Any) -> str:
    """One line for an error ``detail`` that isn't a string (FastAPI's 422 list...)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "msg" in item:
                loc = ".".join(str(p) for p in item.get("loc") or ())
                parts.append(f"{loc}: {item['msg']}" if loc else str(item["msg"]))
            else:
                parts.append(_readable(item))
        return "; ".join(p for p in parts if p)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _passthrough(resp: httpx.Response) -> Any:
    """The API's status and JSON body as they are; a non-JSON body becomes ``detail``.

    An error answer (4xx/5xx) always has a string ``detail``, which is what the hub
    reads: a list ``detail`` (FastAPI's 422 validation errors) is joined into one line
    and kept as ``errors``; a non-object JSON error is wrapped (QA BUG-23).
    """
    from starlette.responses import JSONResponse

    try:
        body = resp.json()
    except ValueError:
        body = {"detail": (resp.text or resp.reason_phrase or "")[:500]}
    if resp.status_code >= 400:
        if not isinstance(body, dict):
            body = {"detail": _readable(body)[:500]}
        elif not isinstance(body.get("detail"), str):
            raw = body.get("detail")
            body = dict(body)
            if isinstance(raw, list):
                body.setdefault("errors", raw)
            msg = body.get("message")
            body["detail"] = (_readable(raw) or (msg if isinstance(msg, str) else ""))[:500]
        if not body["detail"]:
            body["detail"] = resp.reason_phrase or f"HTTP {resp.status_code}"
    return JSONResponse(body, status_code=resp.status_code)


async def _read_capped(request: Any, cap: int | None = None) -> bytes | None:
    """The request body, or None once it grows past ``cap`` (``MAX_BODY_BYTES``)."""
    cap = MAX_BODY_BYTES if cap is None else cap
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        return None
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > cap:
            return None
    return bytes(body)


def add_ingest_routes(mcp: Any, settings: Settings,
                      transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Register the upload routes on ``mcp`` (an ``MCPServer``) for its HTTP app.

    ``transport`` is for tests (an ``httpx.ASGITransport`` over the real API, or a
    ``MockTransport``).
    """
    base = ingest_api_base(settings)
    auth = {"Authorization": f"Bearer {settings.ingest_token}"}

    async def forward(path: str, **kwargs: Any) -> Any:
        return await forward_method("POST", path, **kwargs)

    async def forward_method(method: str, path: str, timeout: httpx.Timeout = TIMEOUT,
                             **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(base_url=base, transport=transport, timeout=timeout,
                                         follow_redirects=False) as client:
                resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            # Never log the request: its URL may carry ?access_token=.
            log.warning("ingest API at %s not reachable: %s", base, type(e).__name__)
            return _json(502, f"Athena's ingest API is not reachable at {base} "
                              "(is `convo-server serve` running?)")
        log.log(logging.WARNING if resp.status_code >= 500 else logging.DEBUG,
                "ingest API answered %s for %s %s", resp.status_code, method, path)
        return _passthrough(resp)

    @mcp.custom_route("/ingest/chunk", methods=["POST"], include_in_schema=False)
    async def ingest_chunk(request: Any) -> Any:
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("multipart/form-data"):
            return _json(415, "send multipart/form-data, like /v1/ingest/chunk")
        body = await _read_capped(request)
        if body is None:
            return _json(413, "chunk too large")
        return await forward("/v1/ingest/chunk", content=body,
                             headers={**auth, "Content-Type": ctype})

    def read_route(method: str, path: str) -> None:
        async def handler(request: Any) -> Any:
            from urllib.parse import quote

            api_path = "/v1" + path if path != "/health" else path
            for name, value in request.path_params.items():
                api_path = api_path.replace("{" + name + "}", quote(str(value), safe=""))
            # The caller's query minus its MCP token; the API gets the ingest token.
            params = [(k, v) for k, v in request.query_params.multi_items()
                      if k != "access_token"]
            kwargs: dict[str, Any] = {"params": params, "headers": dict(auth)}
            if method in ("PATCH", "POST", "PUT"):
                body = await request.body()
                if len(body) > MAX_JSON_BYTES:
                    return _json(413, "request too large")
                kwargs["content"] = body
                kwargs["headers"]["Content-Type"] = "application/json"
            return await forward_method(method, api_path, **kwargs)

        mcp.custom_route(path, methods=[method], include_in_schema=False)(handler)

    for method, path in READ_ROUTES:
        read_route(method, path)

    @mcp.custom_route("/ingest/text", methods=["POST"], include_in_schema=False)
    async def ingest_text(request: Any) -> Any:
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("application/json"):
            return _json(415, "send application/json, like /v1/ingest/text")
        body = await _read_capped(request, MAX_TEXT_BODY_BYTES)
        if body is None:
            return _json(413, "note too large")
        return await forward("/v1/ingest/text", content=body,
                             headers={**auth, "Content-Type": "application/json"})

    @mcp.custom_route("/ingest/text/{source_id}", methods=["DELETE"], include_in_schema=False)
    async def delete_text(request: Any) -> Any:
        from urllib.parse import quote

        source_id = request.path_params["source_id"]
        return await forward_method("DELETE", f"/v1/ingest/text/{quote(source_id, safe='')}",
                                    headers=auth)

    @mcp.custom_route("/sessions/{session_id}/end", methods=["POST"], include_in_schema=False)
    async def end_session(request: Any) -> Any:
        from urllib.parse import quote

        session_id = request.path_params["session_id"]
        return await forward(f"/v1/sessions/{quote(session_id, safe='')}/end", headers=auth)

    # ------------------------------------------------------------------- export
    @mcp.custom_route("/conversations/{conv_id}/export", methods=["GET"],
                      include_in_schema=False)
    async def export_conversation(request: Any) -> Any:
        """The file as the API sends it (not JSON); an error is JSON like the others."""
        from urllib.parse import quote

        from starlette.responses import Response

        conv_id = request.path_params["conv_id"]
        params = [(k, v) for k, v in request.query_params.multi_items()
                  if k in ("format", "include")]
        try:
            async with httpx.AsyncClient(base_url=base, transport=transport, timeout=TIMEOUT,
                                         follow_redirects=False) as client:
                resp = await client.get(f"/v1/conversations/{quote(conv_id, safe='')}/export",
                                        params=params, headers=auth)
        except httpx.HTTPError as e:
            log.warning("ingest API at %s not reachable: %s", base, type(e).__name__)
            return _json(502, f"Athena's ingest API is not reachable at {base} "
                              "(is `convo-server serve` running?)")
        if resp.status_code != 200:
            return _passthrough(resp)
        keep = ("content-type", "content-disposition", "cache-control",
                "x-content-type-options")
        return Response(resp.content, status_code=200,
                        headers={k: v for k, v in resp.headers.items() if k.lower() in keep})

    # ----------------------------------------------------------------- meetings
    @mcp.custom_route("/meetings", methods=["POST"], include_in_schema=False)
    async def join_meeting(request: Any) -> Any:
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("application/json"):
            return _json(415, "send application/json, like /v1/meetings")
        body = await _read_capped(request, MAX_MEETING_BODY_BYTES)
        if body is None:
            return _json(413, "request too large")
        return await forward_method("POST", "/v1/meetings", timeout=MEETING_TIMEOUT,
                                    content=body,
                                    headers={**auth, "Content-Type": "application/json"})

    @mcp.custom_route("/meetings", methods=["GET"], include_in_schema=False)
    async def list_meetings(request: Any) -> Any:
        params: dict[str, int] = {}
        raw = request.query_params.get("limit")
        if raw is not None:
            if not raw.strip().isdigit():
                return _json(400, f"limit must be a whole number from 1 to {MEETING_LIST_MAX}")
            params["limit"] = max(1, min(int(raw), MEETING_LIST_MAX))
        return await forward_method("GET", "/v1/meetings", timeout=MEETING_TIMEOUT,
                                    params=params, headers=auth)

    def meeting_path(request: Any) -> str | None:
        mid = request.path_params["meeting_id"]
        return f"/v1/meetings/{mid}" if _MEETING_ID.fullmatch(mid) else None

    @mcp.custom_route("/meetings/{meeting_id}", methods=["GET"], include_in_schema=False)
    async def get_meeting(request: Any) -> Any:
        path = meeting_path(request)
        if path is None:  # the API's own answer for an id it doesn't know
            return _json(404, "meeting not found")
        return await forward_method("GET", path, timeout=MEETING_TIMEOUT, headers=auth)

    @mcp.custom_route("/meetings/{meeting_id}", methods=["DELETE"], include_in_schema=False)
    async def leave_meeting(request: Any) -> Any:
        path = meeting_path(request)
        if path is None:
            return _json(404, "meeting not found")
        return await forward_method("DELETE", path, timeout=MEETING_LEAVE_TIMEOUT,
                                    headers=auth)

    # ------------------------------------------------------------- Zoom sign-in
    @mcp.custom_route("/zoom/signin", methods=["GET"], include_in_schema=False)
    async def zoom_signin_status(request: Any) -> Any:
        return await forward_method("GET", "/v1/zoom/signin", timeout=MEETING_TIMEOUT,
                                    headers=auth)

    @mcp.custom_route("/zoom/signin/start", methods=["POST"], include_in_schema=False)
    async def zoom_signin_start(request: Any) -> Any:
        return await forward_method("POST", "/v1/zoom/signin/start",
                                    timeout=MEETING_TIMEOUT, headers=auth)

    @mcp.custom_route("/zoom/signin/finish", methods=["POST"], include_in_schema=False)
    async def zoom_signin_finish(request: Any) -> Any:
        # Never log the body: it is the pasted sign-in code.
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("application/json"):
            return _json(415, "send application/json, like /v1/zoom/signin/finish")
        body = await _read_capped(request, MAX_SIGNIN_BODY_BYTES)
        if body is None:
            return _json(413, "request too large")
        return await forward_method("POST", "/v1/zoom/signin/finish",
                                    timeout=MEETING_TIMEOUT, content=body,
                                    headers={**auth, "Content-Type": "application/json"})
