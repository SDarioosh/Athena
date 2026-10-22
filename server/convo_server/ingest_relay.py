"""Upload routes on the MCP HTTP server, forwarded to the ingest API.

Odysseus (the hub) reaches this module only through the MCP server and its token. To let
it add recordings (the browser call recorder, phones paired through Odysseus) without a
second URL and token, the MCP HTTP app gets two plain HTTP routes:

    POST /ingest/chunk               same multipart fields and answer as /v1/ingest/chunk
    POST /sessions/{session_id}/end  same as /v1/sessions/{id}/end

The phone relay's T5 sync, paired through Odysseus, also reads a few API routes the same
way (:data:`READ_ROUTES`): the same paths without ``/v1``, same query, same JSON.

They are not MCP tools (the model never sees them) and sit behind the same ``BearerAuth``
as ``/mcp``; the MCP token can already read all of this through the tools. Each request is
forwarded unchanged to this module's own ingest API with ``CONVO_INGEST_TOKEN``, so there
is one ingest code path and the ingest token (which also signs transcript links) never
leaves the module.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .api import MAX_CHUNK_BYTES
from .config import Settings

log = logging.getLogger(__name__)

# Room for the multipart form fields and boundaries around the audio.
MAX_BODY_BYTES = MAX_CHUNK_BYTES + 256 * 1024
# A 30 s chunk is decoded and run through VAD before the API answers.
TIMEOUT = httpx.Timeout(120.0, connect=5.0)
_ANY_ADDRESS = {"", "0.0.0.0", "::", "[::]"}
MAX_JSON_BYTES = 64 * 1024

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


def _passthrough(resp: httpx.Response) -> Any:
    """The API's status and JSON body as they are; a non-JSON body becomes ``detail``."""
    from starlette.responses import JSONResponse

    try:
        body = resp.json()
    except ValueError:
        body = {"detail": (resp.text or resp.reason_phrase or "")[:500]}
    return JSONResponse(body, status_code=resp.status_code)


async def _read_capped(request: Any) -> bytes | None:
    """The request body, or None once it grows past ``MAX_BODY_BYTES``."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > MAX_BODY_BYTES:
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

    async def forward_method(method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(base_url=base, transport=transport, timeout=TIMEOUT,
                                         follow_redirects=False) as client:
                resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            # Never log the request: its URL may carry ?access_token=.
            log.warning("ingest API at %s not reachable: %s", base, type(e).__name__)
            return _json(502, f"odysseus-sound's ingest API is not reachable at {base} "
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
            if method in ("PATCH", "POST"):
                body = await request.body()
                if len(body) > MAX_JSON_BYTES:
                    return _json(413, "request too large")
                kwargs["content"] = body
                kwargs["headers"]["Content-Type"] = "application/json"
            return await forward_method(method, api_path, **kwargs)

        mcp.custom_route(path, methods=[method], include_in_schema=False)(handler)

    for method, path in READ_ROUTES:
        read_route(method, path)

    @mcp.custom_route("/sessions/{session_id}/end", methods=["POST"], include_in_schema=False)
    async def end_session(request: Any) -> Any:
        from urllib.parse import quote

        session_id = request.path_params["session_id"]
        return await forward(f"/v1/sessions/{quote(session_id, safe='')}/end", headers=auth)
