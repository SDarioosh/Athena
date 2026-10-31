"""The routes the messages relay calls, and how text threads read in the MCP tools."""

from __future__ import annotations

import json
from urllib.parse import quote

from athena.mcp_server import build_mcp
from tests.unit.mcp.test_mcp import call

from .helpers import GROUP, LEO, MIA, MIA_CHAT, T0, H, msg, thread_ids, upload


def test_upload_route_answers_counts_and_400s_name_the_field(client):
    r = client.post("/v1/ingest/messages", json=upload(msg("g1", T0, "Dinner at 7?")))
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["messages"]["added"] == 1
    # the relay's server check: an empty upload without handles or chats
    r = client.post("/v1/ingest/messages", json={"source": "imessage", "messages": []})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/v1/ingest/messages", json={"source": "imessage",
                                                 "messages": [msg("g2", T0, chat="nope")]})
    assert r.status_code == 400 and "messages[0].chat" in r.json()["detail"]
    r = client.post("/v1/ingest/messages", content=b"not json")
    assert r.status_code == 400
    assert client.post("/v1/ingest/messages", json={}, headers={"Authorization": "x"}
                       ).status_code == 401


def test_delete_route_takes_the_url_encoded_chat_id(client):
    client.post("/v1/ingest/messages", json=upload(msg("g1", T0, "hi")))
    url = f"/v1/ingest/messages/chats/imessage/{quote(MIA_CHAT, safe='')}"
    assert client.delete(url).json() == {"ok": True, "result": "deleted", "threads": 1,
                                         "messages": 1}
    assert client.delete(url).json() == {"ok": True, "result": "already"}
    stats = client.get("/v1/texts").json()
    assert stats["chats"] == 0 and stats["messages"] == 0


def test_search_conversations_kind_text_and_person(pipeline):
    pipeline.ingest_messages(upload(
        msg("g1", T0, "Can you bring the lease papers tomorrow?"),
        msg("g2", T0 + 60, "Will do", from_me=True)))
    mcp = build_mcp(pipeline.store, pipeline.settings)
    for kind in ("text", "texts", "iMessage", "sms"):
        out = call(mcp, "search_conversations", kind=kind, person="Mia Chen")
        assert out.startswith("1 text threads"), out
        assert '"Texts with Mia Chen" (Text messages with Mia Chen) id' in out
    assert call(mcp, "search_conversations", kind="in_person").startswith("No conversations")
    out = call(mcp, "search_conversations", query="lease papers")
    assert "(Text messages with Mia Chen)" in out


def test_transcript_shows_when_each_message_was_sent(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "Running late"),
                                    msg("g2", T0 + 2 * H, "No worries", from_me=True)))
    conv = thread_ids(pipeline.store)[0]
    out = call(build_mcp(pipeline.store, pipeline.settings), "get_transcript",
               conversation_id=conv)
    assert '"Texts with Mia Chen" (Text messages with Mia Chen) 2025-10-09 08:53:20' in out
    assert "[0s, Thu 08:53] Mia Chen: Running late" in out
    assert "[7200s, Thu 10:53] Me: No worries" in out


def test_search_transcripts_marks_the_thread(pipeline):
    pipeline.ingest_messages(upload(
        msg("g1", T0, "Saturday hike?", chat=GROUP), msg("g2", T0 + 9, "In!", chat=GROUP,
                                                          handle=LEO),
        names={MIA: "Mia Chen", LEO: "Leo Park"},
        chats=[{"id": GROUP, "name": "Weekend crew", "group": True, "handles": [MIA, LEO]}]))
    out = call(build_mcp(pipeline.store, pipeline.settings), "search_transcripts",
               query="hike")
    assert "Mia Chen: \"Saturday hike?\"" in out and "(Group text: Weekend crew)" in out


def test_list_people_json_has_the_handles(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "hi")))
    people = json.loads(call(build_mcp(pipeline.store, pipeline.settings), "list_people",
                             format="json"))["people"]
    mia = next(p for p in people if p["name"] == "Mia Chen")
    assert mia["handles"] == [{"source": "imessage", "handle": MIA}]


def test_facts_and_items_from_texts_say_so_in_json(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "Can you bring the lease papers tomorrow?")))
    conv = thread_ids(pipeline.store)[0]
    pipeline.store.save_summary(conv, {
        "title": "Lease papers", "summary": "s", "key_points": [], "people": [], "topics": [],
        "action_items": [{"text": "Bring the lease papers", "owner": "Me", "at_seconds": 0}],
        "facts": [{"subject": "Mia Chen", "fact": "Mia Chen is moving flats",
                   "at_seconds": 0}]})
    mcp = build_mcp(pipeline.store, pipeline.settings)
    [item] = json.loads(call(mcp, "list_action_items", format="json"))
    assert item["source"] == "text"
    [fact] = json.loads(call(mcp, "list_facts", format="json"))["facts"]
    assert fact["source"] == "text"
    assert "(Text messages with Mia Chen)" in call(mcp, "list_facts")


def test_view_page_and_unsummarized_threads_read_well(client, pipeline):
    from athena.links import sign

    pipeline.ingest_messages(upload(msg("g1", T0, "ok"), msg("g2", T0 + 26 * H, "k",
                                                            from_me=True)))
    convs = thread_ids(pipeline.store)
    pipeline.summarize_pending(now=T0 + 30 * H)  # both too short for a summary
    conv = convs[0]
    page = client.get(f"/view/{conv}?sig={sign(pipeline.settings, conv)}").text
    assert "Texts with Mia Chen" in page and "Text messages with Mia Chen" in page
    assert "A short exchange; no summary." in page and "Thu 08:53" in page
    out = call(build_mcp(pipeline.store, pipeline.settings), "get_conversation",
               conversation_id=conv)
    assert out.startswith('"Texts with Mia Chen" (Text messages with Mia Chen)')
    assert "Summary: (none: a short exchange; read it with get_transcript)" in out
