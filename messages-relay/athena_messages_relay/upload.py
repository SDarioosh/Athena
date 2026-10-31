"""Talking to the Athena server: uploads and chat removals, with retries."""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

log = logging.getLogger(__name__)


class UploadError(Exception):
    """The server refused the request (4xx): retrying won't help. ``status`` is the HTTP
    status (400: the upload itself is wrong; 401/404: the token or URL is)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class ServerUnavailable(Exception):
    """The server could not be reached, or failed (5xx), after every retry."""


class Uploader:
    def __init__(self, url: str, token: str, *, timeout: float = 60.0, retries: int = 4,
                 backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep

    def post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self.url, json.dumps(payload).encode("utf-8"))

    def delete_chat(self, source: str, chat_id: str) -> Dict[str, Any]:
        url = self.url + "/chats/" + quote(source, safe="") + "/" + quote(chat_id, safe="")
        return self._request("DELETE", url, None)

    def _request(self, method: str, url: str, body: Optional[bytes]) -> Dict[str, Any]:
        delay = self.backoff
        last: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(delay)
                delay *= 2
            req = urllib.request.Request(url, data=body, method=method, headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                detail = _detail(e)
                if 400 <= e.code < 500:
                    raise UploadError("HTTP %d: %s" % (e.code, detail), e.code) from None
                last = ServerUnavailable("HTTP %d: %s" % (e.code, detail))
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                last = ServerUnavailable(str(getattr(e, "reason", e)))
            except ValueError as e:  # not JSON: something else answered
                raise UploadError("the server's answer is not JSON (%s): is the URL right?"
                                  % e) from None
            else:
                if not data.get("ok", False):
                    raise UploadError("the server answered without ok: %s" % data)
                return data
            log.warning("%s failed (attempt %d): %s", method, attempt + 1, last)
        assert last is not None
        raise last


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        return e.reason if isinstance(e.reason, str) else ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or data)[:300]
    return str(data)[:300]
