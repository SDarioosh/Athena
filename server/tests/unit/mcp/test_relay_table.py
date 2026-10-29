"""Each route the relay forwards to exists in the ingest API (method and path).

A route renamed or removed in ``athena/api.py`` without its relay entry fails here,
instead of the hub getting a 404 or 405 from the relay.
"""

from __future__ import annotations

import re

import pytest
from starlette.routing import Match

from athena.api import create_app
from athena.ingest_relay import FORWARD_ROUTES, READ_ROUTES

# The relay routes written out by hand in add_ingest_routes: (method, API path).
HAND_ROUTES = [
    ("POST", "/v1/ingest/chunk"),
    ("POST", "/v1/ingest/text"),
    ("DELETE", "/v1/ingest/text/{source_id}"),
    ("POST", "/v1/sessions/{session_id}/end"),
    ("GET", "/v1/conversations/{conv_id}/export"),
]


def _api_path(path: str) -> str:
    return path if path == "/health" else "/v1" + path


@pytest.fixture
def api(pipeline):
    return create_app(pipeline)


def in_api(app, method: str, path: str) -> bool:
    """Whether the API's router fully matches ``method`` on ``path`` (each parameter
    filled with a sample value), the way it routes a real request."""
    scope = {"type": "http", "method": method, "path": re.sub(r"\{[^}]*\}", "x1", path),
             "root_path": "", "query_string": b"", "headers": []}
    return any(route.matches(scope)[0] == Match.FULL for route in app.router.routes)


def relay_entries() -> list[tuple[str, str]]:
    return ([(m, _api_path(p)) for m, p in READ_ROUTES]
            + [(r.method, "/v1" + r.path) for r in FORWARD_ROUTES] + HAND_ROUTES)


@pytest.mark.parametrize("method,path", relay_entries(),
                         ids=[f"{m} {p}" for m, p in relay_entries()])
def test_relay_entry_exists_in_the_api(api, method: str, path: str) -> None:
    assert in_api(api, method, path), f"{method} {path} is not an API route"


def test_a_renamed_route_would_fail(api) -> None:
    assert not in_api(api, "GET", "/v1/meeting/{meeting_id}")
    assert not in_api(api, "PUT", "/v1/meetings/{meeting_id}")
    assert not in_api(api, "GET", "/meetings")


def test_table_has_the_meeting_and_zoom_routes() -> None:
    assert [(r.method, r.path) for r in FORWARD_ROUTES] == [
        ("POST", "/meetings"), ("GET", "/meetings"), ("GET", "/meetings/{meeting_id}"),
        ("DELETE", "/meetings/{meeting_id}"), ("GET", "/zoom/signin"),
        ("POST", "/zoom/signin/start"), ("POST", "/zoom/signin/finish")]
