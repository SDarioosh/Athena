"""The hub client: everything goes through Odysseus' token routes with the one paired token.

The PC relay's classes are reused as they are, with two changes made here by subclassing:

- `HubApi` is `pda_feed.HttpApi` (the `ServerApi` of `PdaSync`) on the hub's Athena relay
  routes: a module path `/v1/<x>` goes to `/api/codex/athena/<x>`, and `health()` is
  `GET /api/codex/athena/health` **with** the token (the hub's health needs it and also
  checks that the module answers). A hub from before the Athena names has only
  `/api/codex/sound/<x>`: as in the Android relay (`HubRoutes`), a 404 on a hub not yet
  known falls back to the old path once, and the first answer that proves a route exists
  decides which paths the hub has.
- every client (`HubApi`, `HubCalendar`, `HubTodos`, `HubInsight`) uses an opener with the
  configured HTTP proxy (Tailscale's userspace mode: `http://127.0.0.1:1055`), or no proxy
  at all (proxy variables in the environment are ignored, so a request never goes
  somewhere the owner didn't set). Redirects are never followed, as in `Uploader`. Every
  request's URL is checked against `addresses` first (and, without a proxy, the addresses
  its host resolves to): never this device, link-local or cloud metadata.

The token goes only in the `Authorization` header (`Uploader._request`); it's never logged
and never in a URL or a repr.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from athena_relay.pda_calendar import OdysseusCalendar
from athena_relay.pda_feed import HttpApi
from athena_relay.pda_insight import OdysseusInsight
from athena_relay.pda_todos import OdysseusTodos
from athena_relay.uploader import UploadError

from . import addresses

log = logging.getLogger("athena_pda_sync.hub")

RELAY = "/api/codex/athena"
OLD_RELAY = "/api/codex/sound"
HEALTH = RELAY + "/health"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib raises HTTPError with the 3xx status


class AddressCheck(urllib.request.BaseHandler):
    """Refuses a request to an address of `addresses` before anything is sent (URLError)."""

    handler_order = 100  # before every other handler's preprocessing

    def __init__(self, resolve: bool) -> None:
        self.resolve = resolve

    def _check(self, req: urllib.request.Request) -> urllib.request.Request:
        parts = urllib.parse.urlsplit(req.full_url)
        try:
            addresses.check_url(req.full_url)
            if self.resolve and parts.hostname:
                addresses.check_resolved(parts.hostname, parts.port)
        except (addresses.RefusedAddress, ValueError) as e:
            raise urllib.error.URLError(str(e)) from None
        return req

    http_request = https_request = _check


def make_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    """No redirects; the given proxy for http and https, or none (never the environment's);
    no request to this device, link-local or cloud metadata (AddressCheck)."""
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(AddressCheck(resolve=not proxy), NoRedirect,
                                       urllib.request.ProxyHandler(proxies))


def _proves_route(status: int | None) -> bool:
    """An answer that shows the route exists (HubRoutes.provesRoute in the Android relay)."""
    return status is not None and 200 <= status <= 499 and status not in (401, 404, 408, 429)


class HubApi(HttpApi):
    """`ServerApi` (pda_feed) through the hub's Athena relay routes."""

    def __init__(self, server: str, token: str, *, proxy: str | None = None,
                 timeout: float = 25.0) -> None:
        super().__init__(server, token, timeout=timeout)
        self._opener = make_opener(proxy)
        self.old_routes: bool | None = None  # None: not known yet
        # the last health() failure, for the wording of "Hub not reachable" / "Hub answered
        # HTTP 502" on Status (PdaSync reports a refused token itself: AUTH_FAILED)
        self.last_health_error: UploadError | None = None

    def __repr__(self) -> str:
        return f"HubApi({self.server!r})"

    def health(self) -> None:
        """Raise UploadError unless the hub and the module answer (needs the token)."""
        try:
            self._request("GET", HEALTH)
        except UploadError as e:
            self.last_health_error = e
            raise
        self.last_health_error = None

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if path.startswith("/v1/"):
            path = RELAY + path[3:]
        if not path.startswith(RELAY + "/"):
            return super()._request(method, path, **kw)
        old = OLD_RELAY + path[len(RELAY):]
        if self.old_routes is not None:
            return super()._request(method, old if self.old_routes else path, **kw)
        try:
            answer = super()._request(method, path, **kw)
        except UploadError as e:
            if e.status != 404:
                if _proves_route(e.status):
                    self.old_routes = False
                raise
        else:
            self.old_routes = False
            return answer
        try:
            answer = super()._request(method, old, **kw)
        except UploadError as e:
            if _proves_route(e.status):
                self.old_routes = True
                log.info("this Odysseus has only the old /api/codex/sound routes")
            raise
        self.old_routes = True
        log.info("this Odysseus has only the old /api/codex/sound routes")
        return answer


class HubCalendar(OdysseusCalendar):
    def __init__(self, server: str, token: str, *, calendar_href: str | None = None,
                 proxy: str | None = None, timeout: float = 25.0) -> None:
        super().__init__(server, token, calendar_href=calendar_href, timeout=timeout)
        self._opener = make_opener(proxy)


class HubTodos(OdysseusTodos):
    def __init__(self, server: str, token: str, *, proxy: str | None = None,
                 timeout: float = 25.0) -> None:
        super().__init__(server, token, timeout=timeout)
        self._opener = make_opener(proxy)


class HubInsight(OdysseusInsight):
    def __init__(self, server: str, token: str, *, proxy: str | None = None,
                 timeout: float = 25.0) -> None:
        super().__init__(server, token, timeout=timeout)
        self._opener = make_opener(proxy)


@dataclass
class Hub:
    """The four clients of one sync, all on the paired hub URL and token."""

    api: HubApi
    calendar: HubCalendar
    todos: HubTodos
    insight: HubInsight

    @classmethod
    def make(cls, url: str, token: str, *, calendar_href: str | None = None,
             proxy: str | None = None, timeout: float = 25.0) -> Hub:
        """Raises ValueError for a bad URL or token (also a refused address)."""
        addresses.check_url(url)
        return cls(HubApi(url, token, proxy=proxy, timeout=timeout),
                   HubCalendar(url, token, calendar_href=calendar_href, proxy=proxy,
                               timeout=timeout),
                   HubTodos(url, token, proxy=proxy, timeout=timeout),
                   HubInsight(url, token, proxy=proxy, timeout=timeout))
