"""MCP knowledge layer: tools over the store, speaker naming, signed transcript links."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server.config import Settings
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
        "list_people", "name_speaker", "rename_speaker_everywhere", "reassign_line",
        "list_unnamed_speakers"}


AGENTS_MD = Path(__file__).resolve().parents[2] / "AGENTS.md"


def contract_table() -> dict[str, str]:
    """Tool name -> "ro" or "rw", read from the "MCP tool contract" table in AGENTS.md."""
    section = AGENTS_MD.read_text().split("## MCP tool contract", 1)[1].split("\n## ", 1)[0]
    table: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not cells[0].startswith("`"):
            continue
        kind = ("ro" if cells[1].startswith("read-only") else
                "rwd" if cells[1].startswith("write, destructive") else
                "rw" if cells[1].startswith("write") else None)
        assert kind, f"unknown annotation in AGENTS.md: {cells[1]!r}"
        for name in re.findall(r"`([a-z_]+)`", cells[0]):
            table[name] = kind
    return table


def test_tool_annotations_match_contract(mcp):
    """Every tool is in the AGENTS.md contract table with exactly its annotation.

    Odysseus runs readOnlyHint tools without approval, so a new tool must be added to the
    table (and coordinated with Odysseus) before this passes.
    """
    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    contract = contract_table()
    assert len(contract) == 13  # the table parsed: 9 read-only + 4 write tools
    tools = {t.name: t.annotations for t in asyncio.run(go())}
    assert set(tools) == set(contract), (
        f"not in the AGENTS.md table: {sorted(set(tools) - set(contract))}; "
        f"in the table but not served: {sorted(set(contract) - set(tools))}")
    for name, kind in contract.items():
        a = tools[name]
        assert a is not None, f"{name} has no annotations"
        if kind == "ro":
            assert a.read_only_hint is True, f"{name} must be readOnlyHint: true"
        else:
            assert a.read_only_hint is False, f"{name} must be readOnlyHint: false"
            assert a.destructive_hint is (kind == "rwd"), (
                f"{name} must be destructiveHint: {kind == 'rwd'}")


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


def test_every_linked_result_asks_for_citations(pipeline, mcp):
    # Frontends drop the server instructions, so each result with links must carry the hint.
    process_two_speakers(pipeline)
    conv_id = pipeline.store.list_conversations()[0]["id"]
    pipeline.make_digest(DAY)
    hint = "put the link next to each fact you use"
    for tool, args in [("search_transcripts", {"speaker": "Me"}),
                       ("search_conversations", {"since": DAY}),
                       ("get_conversation", {"conversation_id": conv_id}),
                       ("get_transcript", {"conversation_id": conv_id}),
                       ("list_action_items", {}),
                       ("list_facts", {"subject": "alice"}),
                       ("get_daily_digest", {"date": DAY})]:
        assert hint in call(mcp, tool, **args), tool


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


def test_placeholder_mcp_token_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="CONVO_MCP_TOKEN"):
        Settings(ingest_token="real-token-0123456789", data_dir=tmp_path,
                 mcp_token="change-me-generate-another-long-random-token")
    assert Settings(ingest_token="real-token-0123456789", data_dir=tmp_path).mcp_token is None
