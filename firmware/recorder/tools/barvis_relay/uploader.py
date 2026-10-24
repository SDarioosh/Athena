"""Client for the Athena server's ingest API, standard library only.

- `GET /health` (no token) before any chunk is pulled (§6 step 5).
- `POST /v1/ingest/chunk` (multipart) and `POST /v1/sessions/{id}/end`, bearer token.

Every failure is an `UploadError` with a `kind` that decides what the relay does
(docs/twatch-ble-protocol.md §6.6.5):

- `RETRY`: network error, timeout, 5xx, 408, 429 (and anything unexpected): stop draining,
  nothing is acked, try again later with backoff.
- `AUTH`: 401/403: stop, the token is rejected.
- `SKIP`: any other 4xx: leave the chunk on the watch, skip it for this connection.

The token is only ever put in the Authorization header. It's never logged, never part of a
URL, and redirects are not followed (urllib would forward the header to the new host).
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

log = logging.getLogger("barvis_relay.uploader")

RETRY = "retry"
AUTH = "auth"
SKIP = "skip"


class UploadError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def classify(status: int) -> str:
    if status in (401, 403):
        return AUTH
    if status in (408, 429) or status >= 500:
        return RETRY
    if 400 <= status < 500:
        return SKIP
    return RETRY  # 1xx/3xx/odd 2xx: unexpected, don't ack, try again later


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


def multipart(fields: dict[str, str], file_field: str, filename: str, content_type: str,
              data: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body, Content-Type header value)."""
    boundary = "barvis-" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    out += data
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class Uploader:
    def __init__(self, server: str, token: str, *, timeout: float = 25.0) -> None:
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("server must be an http:// or https:// URL")
        if parsed.username or parsed.password or parsed.query:
            raise ValueError("server URL must not contain credentials or a query")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("token is empty or contains a line break")
        self.server = server.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:  # keep the token out of any repr
        return f"Uploader({self.server!r})"

    # -------------------------------------------------------------- requests

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str | None = None, auth: bool = True) -> dict[str, Any]:
        url = self.server + path
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if auth:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = _detail(e)
            raise UploadError(classify(e.code), f"{method} {path}: HTTP {e.code} {detail}",
                              e.code) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            # HTTPException: a cut-short body (IncompleteRead) or a non-HTTP answer
            # (BadStatusLine) is not an OSError; it must not escape Relay.run.
            reason = getattr(e, "reason", e)
            raise UploadError(RETRY, f"{method} {path}: {type(e).__name__}: {reason}") from None
        if status != 200:
            raise UploadError(classify(status), f"{method} {path}: HTTP {status}", status)
        try:
            answer = json.loads(raw or b"{}")
        except ValueError:
            raise UploadError(RETRY, f"{method} {path}: answer is not JSON", status) from None
        if not isinstance(answer, dict):
            raise UploadError(RETRY, f"{method} {path}: answer is not a JSON object", status)
        return answer

    def health(self) -> None:
        """Raise UploadError(RETRY/...) unless GET /health answers 200."""
        self._request("GET", "/health", auth=False)

    def upload_chunk(self, *, device: str, session_id: str, sequence: int, timestamp: str,
                     audio: bytes, kind: str | None = None) -> dict[str, Any]:
        """POST /v1/ingest/chunk. Returns the server's answer, which has `"ok": true`.

        `kind` ("note" for a voice note) is sent only when given, so ordinary uploads stay
        as they were (the server defaults to "conversation")."""
        fields = {"device": device, "session_id": session_id, "sequence": str(sequence),
                  "timestamp": timestamp, "codec": "ogg"}
        if kind is not None:
            fields["kind"] = kind
        body, ctype = multipart(
            fields,
            "audio", f"{session_id}-{sequence}.opus", "audio/ogg", audio)
        answer = self._request("POST", "/v1/ingest/chunk", body=body, content_type=ctype)
        if answer.get("ok") is not True:
            raise UploadError(RETRY, f"ingest answered without ok: {_short(answer)}", 200)
        return answer

    def end_session(self, session_id: str) -> dict[str, Any]:
        """POST /v1/sessions/{id}/end."""
        path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/end"
        answer = self._request("POST", path, body=b"")
        if answer.get("ok") is not True:
            raise UploadError(RETRY, f"end answered without ok: {_short(answer)}", 200)
        return answer


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read(2000)
        detail = json.loads(raw).get("detail", "")
    except Exception:  # only for the message
        return ""
    return _short(detail)


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return " ".join(text.split())[:200]
