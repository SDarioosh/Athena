"""MCP knowledge layer: tools over the store, speaker naming, signed transcript links."""

from __future__ import annotations

import asyncio
import re
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server.links import sign, transcript_link
from convo_server.mcp_server import BearerAuth, build_mcp, parse_when

T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC
DAY = "2025-10-09"


def ingest(pipeline, audio, session="s1", seq=0, start=T0):
    pipeline.ingest_chunk(device="omi", session_id=session, sequence=seq, started_at=start,
                          codec="wav", data=wav(audio))


def process_two_speakers(pipeline, session="s1", start=T0):
    # owner (200 Hz) then an unknown voice (600 Hz)
    ingest(pipeline, np.concatenate([tone(200, 4), silence(4), tone(600, 4), silence(18)]),
           session=session, start=start)
    pipeline.end_session(session)
    pipeline.process_pending()


def call(mcp, tool, **args) -> str:
    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            res = await client.call_tool(tool, args)
            return "\n".join(c.text for c in res.content if hasattr(c, "text"))

    return asyncio.run(go())


@pytest.fixture
def mcp(pipeline):
    return build_mcp(pipeline.store, pipeline.settings)


def test_parse_when():
    tz = ZoneInfo("Europe/Berlin")
    assert parse_when("", tz) is None
    start, end = parse_when("2026-10-18", tz), parse_when("2026-10-18", tz, end=True)
    assert end - start == 86400
    assert parse_when("2026-10-18T10:00:00Z", tz) == 1792317600.0
    with pytest.raises(ValueError):
        parse_when("last tuesday", tz)


def test_lists_tools(mcp):
    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(go()) >= {
        "search_transcripts", "search_conversations", "get_conversation", "get_transcript",
        "list_action_items", "update_action_item", "list_facts", "get_daily_digest",
        "list_people", "name_speaker"}


def test_what_did_x_say_on_date(pipeline, mcp):
    process_two_speakers(pipeline)
    out = call(mcp, "search_transcripts", speaker="me", since=DAY, until=DAY)
    assert "Me: \"words at 200 hz\"" in out and "/view/" in out and "sig=" in out
    assert "600" not in out  # only the requested speaker's lines
    assert call(mcp, "search_transcripts", speaker="Me", since="2025-10-10").startswith("No lines")
    assert "Known speakers: " in call(mcp, "search_transcripts", speaker="Alice")
    # text query within a speaker's lines
    assert "200 hz" in call(mcp, "search_transcripts", speaker="Me", query="words")


def test_name_speaker_then_recognised_next_time(pipeline, mcp):
    process_two_speakers(pipeline)
    conv_id = pipeline.store.list_conversations()[0]["id"]
    assert "Speaker 1" in call(mcp, "list_people")
    out = call(mcp, "name_speaker", conversation_id=conv_id, speaker_label="Speaker 1",
               name="Alice")
    assert "Renamed 1 lines" in out and "recognised" in out
    assert "Alice: \"words at 600 hz\"" in call(mcp, "search_transcripts", speaker="Alice")
    assert pipeline.store.get_conversation(conv_id)["status"] == "pending"
    assert pipeline.summarize_pending() == 1
    assert conv_id in call(mcp, "search_conversations", person="Alice")

    # a later recording: Alice's voice is now known without naming her again
    process_two_speakers(pipeline, session="s2", start=T0 + 3600)
    out = call(mcp, "search_transcripts", speaker="Alice", since=DAY, until=DAY)
    assert out.count("Alice:") == 2
    assert "Speaker 1" not in call(mcp, "list_people").split("Mentioned:")[0]


def test_conversation_tools(pipeline, mcp):
    process_two_speakers(pipeline)
    conv_id = pipeline.store.list_conversations()[0]["id"]
    detail = call(mcp, "get_conversation", conversation_id=conv_id)
    assert "Planning the trip" in detail and "Book flights" in detail and "#t=1" in detail
    assert "Speaker 1: words at 600 hz" in call(mcp, "get_transcript", conversation_id=conv_id)
    assert call(mcp, "get_conversation", conversation_id="nope").startswith("No conversation")
    found = call(mcp, "search_conversations", query="vegetarian", since=DAY, until=DAY)
    assert conv_id in found
    assert call(mcp, "search_conversations", query="vegetarian",
                since="2030-01-01").startswith("No conversations")

    items = call(mcp, "list_action_items")
    item_id = int(re.search(r"#(\d+)", items).group(1))
    assert "now done" in call(mcp, "update_action_item", item_id=item_id, status="done")
    assert call(mcp, "list_action_items").startswith("No open action items")
    assert "vegetarian" in call(mcp, "list_facts", subject="alice")
    assert "vegetarian" in call(mcp, "list_facts", query="vegetarian")

    pipeline.make_digest(DAY)
    assert "A day of plans" in call(mcp, "get_daily_digest", date=DAY)
    assert call(mcp, "get_daily_digest", date="2001-01-01").startswith("No digest")


def test_signed_transcript_view(client, pipeline):
    process_two_speakers(pipeline)
    conv_id = pipeline.store.list_conversations()[0]["id"]
    link = transcript_link(pipeline.settings, conv_id, 4)
    assert link.endswith("#t=4")
    client.headers.pop("Authorization")  # browser links carry no bearer token
    path = link.split(str(pipeline.settings.port), 1)[1].split("#")[0]
    page = client.get(path)
    assert page.status_code == 200 and "words at 600 hz" in page.text
    assert "Planning the trip" in page.text
    assert client.get(f"/view/{conv_id}?sig=bad").status_code == 404
    assert client.get(f"/view/nope?sig={sign(pipeline.settings, 'nope')}").status_code == 404
    assert client.get(f"/view/{conv_id}").status_code == 404


def test_http_bearer_auth():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"))])
    c = TestClient(BearerAuth(app, "secret-token-123456"))
    assert c.get("/mcp").status_code == 401
    assert c.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/mcp", headers={"Authorization": "Bearer secret-token-123456"}).text == "ok"


def test_http_query_token_for_oauth_only_clients():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"))])
    c = TestClient(BearerAuth(app, "secret-token-123456"))
    assert c.get("/mcp?access_token=secret-token-123456").text == "ok"
    assert c.get("/mcp?access_token=nope").status_code == 401
    assert c.get("/mcp?access_token=").status_code == 401
