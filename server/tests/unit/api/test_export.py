"""Sharing a conversation as a file: GET /v1/conversations/{id}/export and its relay."""

from __future__ import annotations

import httpx
import pytest

from athena import export
from athena.api import create_app
from tests.conftest import TOKEN
from tests.unit.mcp.test_ingest_relay import MCP_TOKEN, mcp_client
from tests.unit.mcp.test_mcp import process_two_speakers


@pytest.fixture
def conv_id(pipeline) -> str:
    process_two_speakers(pipeline)
    return pipeline.store.list_conversations()[0]["id"]


def test_markdown_has_summary_items_and_transcript_but_no_links(client, pipeline, conv_id):
    r = client.get(f"/v1/conversations/{conv_id}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"].startswith('attachment; filename="')
    assert "Planning the trip.md" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    md = r.text
    assert md.startswith("# Planning the trip\n")
    assert "## Summary" in md and "Alice and the user planned a trip" in md
    assert "- Trip to Lisbon in May" in md
    assert "- [ ] Book flights (Me, due Friday)" in md
    assert "## Transcript" in md and "Speaker 1:** words at 600 hz" in md
    # facts only when asked; nothing that reaches back into the owner's network
    assert "vegetarian" not in md
    assert "sig=" not in md and "/view/" not in md and conv_id not in md
    assert "Shared from Athena" in md


def test_include_picks_sections(client, conv_id):
    md = client.get(f"/v1/conversations/{conv_id}/export?include=transcript").text
    assert "## Transcript" in md and "## Summary" not in md and "Book flights" not in md
    md = client.get(f"/v1/conversations/{conv_id}/export?include=summary,facts").text
    assert "## Facts" in md and "Alice is vegetarian" in md and "## Transcript" not in md
    r = client.get(f"/v1/conversations/{conv_id}/export?include=summary,audio")
    assert r.status_code == 400 and "audio" in r.json()["detail"]


def test_html_is_one_page_without_links_or_script(client, pipeline, conv_id):
    r = client.get(f"/v1/conversations/{conv_id}/export?format=html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert ".html" in r.headers["content-disposition"]
    page = r.text
    assert "Planning the trip" in page and "words at 600 hz" in page
    assert "<script" not in page and "sig=" not in page and "Shared from Athena" in page
    assert "Alice is vegetarian" not in page
    # the owner's own /view page is unchanged: script and facts
    view = export.render_conversation(pipeline.settings, pipeline.store.get_conversation(
        conv_id), pipeline.store.conversation_segments(conv_id))
    assert "<script" in view and "Alice is vegetarian" in view


def test_errors(client, conv_id):
    assert client.get("/v1/conversations/nope/export").status_code == 404
    r = client.get(f"/v1/conversations/{conv_id}/export?format=pdf")
    assert r.status_code == 400 and "format" in r.json()["detail"]
    client.headers.pop("Authorization")
    assert client.get(f"/v1/conversations/{conv_id}/export").status_code == 401


def test_dismissed_items_and_forget_pending_stay_out(client, pipeline, conv_id):
    store = pipeline.store
    item = store.get_conversation(conv_id)["action_items"][0]
    store.update_action_item(item["id"], status="dismissed")
    md = client.get(f"/v1/conversations/{conv_id}/export").text
    assert "Book flights" not in md
    conv = {**store.get_conversation(conv_id), "forget_pending": True}
    md = export.render_markdown(pipeline.settings, conv, [], export.DEFAULT_INCLUDE)
    assert "Planning the trip" not in md and "Lisbon" not in md
    assert "Being rewritten" in md


def test_filename_is_safe():
    from athena.config import Settings

    s = Settings(ingest_token=TOKEN, timezone="UTC")
    conv = {"started_at": 1_760_000_000.0, "title": 'Café / "plans" <b>?'}
    name = export.filename(s, conv, "md")
    assert name == "2025-10-09 Café plans b.md"
    cd = export.content_disposition(name)
    assert 'filename="2025-10-09 Caf_ plans b.md"' in cd and "Caf%C3%A9" in cd
    assert export.filename(s, {**conv, "title": "///"}, "html") == "2025-10-09 Conversation.html"


def test_relay_passes_the_file_through(pipeline, conv_id):
    relay = mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline)))
    path = f"/conversations/{conv_id}/export"
    assert relay.get(path).status_code == 401
    assert relay.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    r = relay.get(f"{path}?access_token={MCP_TOKEN}&format=html&include=summary")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers["content-disposition"]
    assert "Planning the trip" in r.text and "words at 600 hz" not in r.text
    r = relay.get(f"/conversations/nope/export?access_token={MCP_TOKEN}")
    assert r.status_code == 404 and r.json() == {"detail": "conversation not found"}
    r = relay.get(f"{path}?access_token={MCP_TOKEN}&format=doc")
    assert r.status_code == 400 and "format" in r.json()["detail"]
