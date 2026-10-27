"""Meeting chat, topic and participants (Zoom bot lane 3B): the chat route's contract,
chat lines in the meeting's conversation, the summary prompt, MCP output (0.16.0),
erase_fact, retention and reprocessing. Fictional data only."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import TOKEN
from test_mcp import call
from test_meeting_tracks import ALICE, BO, CLEO, T0, ScriptedWhisper

from convo_server.llm import format_transcript
from convo_server.mcp_server import build_mcp
from convo_server.store import CHAT, SPEECH, Segment, Store

SERVER = Path(__file__).resolve().parents[1]
DANA = "16782336"  # in the meeting, typed in chat, never spoke (no track)
LINK = "https://example.org/files/budget-v3.xlsx"

CHAT_BODY = {
    "topic": "Q4 budget review",
    "lines": [
        {"at": T0 + 5, "from": "Dana Example", "zoom_user_id": DANA,
         "text": f"Here is the sheet: {LINK}"},
        {"at": T0 + 12, "from": "Alice Example", "zoom_user_id": ALICE[0],
         "text": "Thanks Dana!"},
        {"at": T0 + 13, "from": "Bo Example", "zoom_user_id": BO[0],
         "text": "Athena, are you recording?", "private": True},
    ],
    "participants": [
        {"zoom_user_id": ALICE[0], "name": "Alice Example", "joined_at": T0,
         "left_at": T0 + 1800},
        {"zoom_user_id": BO[0], "name": "Bo Example", "joined_at": T0},
        {"zoom_user_id": CLEO[0], "name": "cleo  example", "joined_at": T0},
        {"zoom_user_id": DANA, "name": "Dana Example (she/her)", "joined_at": T0 + 2},
        {"zoom_user_id": "16783360", "name": "Rafael Example", "joined_at": T0 + 60,
         "left_at": T0 + 600},
    ],
}


def upload(pipeline, session="m1", tracks=(ALICE, BO, CLEO)):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.settings.zoom_owner_names = "Cleo Example"
    for track, name, data in tracks:
        pipeline.ingest_chunk(device="zoom", session_id=session, sequence=0, started_at=T0,
                              codec="wav", data=data, track=track, track_name=name)


def process(pipeline, session="m1"):
    pipeline.end_session(session)
    pipeline.process_session(session)
    return pipeline.store.session_segments(session)


def chat(client, body=None, session="m1"):
    return client.post(f"/v1/sessions/{session}/chat", json=CHAT_BODY if body is None else body)


@pytest.fixture
def done_meeting(pipeline, client):
    """Chunks, the chat call before the session ends (as the bot may), then processing."""
    upload(pipeline)
    r = chat(client)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "queued"
    segs = process(pipeline)
    return segs


# ------------------------------------------------------------ route contract


def test_unknown_session_is_404_and_plain_session_is_409(pipeline, client):
    assert chat(client, session="nope").status_code == 404
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2])
    r = chat(client, session="s1")
    assert r.status_code == 409 and "not a meeting" in r.text


def test_zoom_device_without_tracks_is_a_meeting(pipeline, client):
    pipeline.ingest_chunk(device="zoom", session_id="z1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2])
    assert chat(client, {"lines": [{"at": T0, "from": "Dana", "text": "hi"}]},
                session="z1").json()["added"] == 1


def test_needs_the_bearer_token(pipeline, client):
    upload(pipeline)
    r = client.post("/v1/sessions/m1/chat", json=CHAT_BODY, headers={"Authorization": ""})
    assert r.status_code == 401


def test_validation_errors(pipeline, client):
    upload(pipeline)
    assert chat(client, {"lines": "nope"}).status_code == 422
    assert chat(client, {"lines": [{"at": T0, "from": "Dana"}]}).status_code == 422  # no text
    r = client.post("/v1/sessions/m1/chat", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    bad_id = {"lines": [{"at": T0, "from": "D", "zoom_user_id": "a b", "text": "x"}]}
    assert chat(client, bad_id).status_code == 400
    bad_at = {"lines": [{"at": "yesterday", "from": "D", "text": "x"}]}
    assert chat(client, bad_at).status_code == 400
    assert chat(client, {"lines": [{"at": 5, "from": "D", "text": "x"}]}).status_code == 400
    assert chat(client, {"participants": [{"name": "D", "joined_at": "soon"}]}).status_code \
        == 400


def test_caps(pipeline, client):
    upload(pipeline)
    many = {"lines": [{"at": T0, "from": "D", "text": "x"}] * 2001}
    assert chat(client, many).status_code == 413
    big = {"lines": [{"at": T0 + i, "from": "D", "text": "y" * 3990} for i in range(260)]}
    assert chat(client, big).status_code == 413
    people = {"participants": [{"name": f"P{i}"} for i in range(1001)]}
    assert chat(client, people).status_code == 413
    # a line over 4,000 characters is cut, control characters and line breaks go
    long = {"lines": [{"at": T0, "from": "Dana\x07", "text": "a\x00b\n" + "z" * 5000}]}
    assert chat(client, long).json()["added"] == 1
    line = next(s for s in pipeline.store.session_segments("m1") if s.kind == CHAT)
    assert line.text.startswith("a b z") and len(line.text) == 4000
    assert line.speaker == "Dana"


def test_resend_is_idempotent_and_private_lines_are_dropped(pipeline, client):
    upload(pipeline)
    first = chat(client).json()
    assert first == {"ok": True, "session_id": "m1", "result": "queued", "added": 2,
                     "duplicates": 0, "private_dropped": 1, "conversation_id": None}
    again = chat(client).json()
    assert (again["result"], again["added"], again["duplicates"]) == ("unchanged", 0, 2)
    chats = [s for s in pipeline.store.session_segments("m1") if s.kind == CHAT]
    assert [s.text for s in chats] == [f"Here is the sheet: {LINK}", "Thanks Dana!"]
    assert not any("recording" in s.text for s in pipeline.store.session_segments("m1"))
    # a resend without topic or participants keeps them
    chat(client, {"lines": CHAT_BODY["lines"]})
    info = pipeline.store._one("SELECT topic, participants FROM meeting_info")
    assert info["topic"] == "Q4 budget review" and len(json.loads(info["participants"])) == 5


# ------------------------------------------------- lines in the conversation


def test_chat_lines_join_the_meeting_in_time_order(done_meeting, pipeline):
    segs = done_meeting
    assert segs == sorted(segs, key=lambda s: s.started_at)
    assert len({s.conversation_id for s in segs}) == 1
    rows = [(s.speaker, s.kind, s.text) for s in segs]
    assert ("Dana Example", CHAT, f"Here is the sheet: {LINK}") in rows
    assert ("Alice Example", CHAT, "Thanks Dana!") in rows  # named like her track
    assert ("Alice Example", SPEECH, "Hello everyone, thanks for joining.") in rows
    dana = next(s for s in segs if s.kind == CHAT and s.speaker == "Dana Example")
    assert dana.started_at == dana.ended_at == T0 + 5
    # the chat sender without a track is a person, found by her Zoom name next time
    assert pipeline.store.person_id("Dana Example") is not None


def test_owner_chat_is_the_owners_line(pipeline, client):
    upload(pipeline)
    chat(client, {"lines": [{"at": T0 + 3, "from": "cleo  example",
                             "zoom_user_id": CLEO[0], "text": "Agenda is in the doc"}]})
    line = next(s for s in process(pipeline) if s.kind == CHAT)
    assert (line.speaker, line.is_owner) == ("Me", True)


def test_owner_chat_without_a_voice_keeps_convo_owner_name(pipeline, client):
    # owner request (QA BUG-07 follow-up): no owner voice; owner chat (with and without a
    # track) is named CONVO_OWNER_NAME, the owner person too, the Zoom name an alias
    pipeline.store._conn.execute("DELETE FROM speakers")
    pipeline.store._conn.execute("DELETE FROM people")
    pipeline.settings.owner_name = "Shahyah"
    upload(pipeline, tracks=(ALICE, BO))
    chat(client, {"lines": [{"at": T0 + 3, "from": "Cleo Example", "zoom_user_id": CLEO[0],
                             "text": "Agenda is in the doc"}]})
    line = next(s for s in process(pipeline) if s.kind == CHAT)
    assert (line.speaker, line.is_owner) == ("Shahyah", True)
    owners = [p for p in pipeline.store.people() if p["is_owner"]]
    assert [p["name"] for p in owners] == ["Shahyah"] and "Cleo Example" in owners[0]["aliases"]


def test_chat_after_processing_is_attached_and_resummarized(pipeline, client, llm_log):
    upload(pipeline)
    process(pipeline)
    conv_id = pipeline.store.session_segments("m1")[0].conversation_id
    assert pipeline.store.get_conversation(conv_id)["status"] == "summarized"
    llm_log.clear()
    r = chat(client).json()
    assert (r["result"], r["conversation_id"], r["added"]) == ("attached", conv_id, 2)
    conv = pipeline.store.get_conversation(conv_id)
    assert conv["status"] == "pending"
    assert pipeline.summarize_pending() == 1
    prompt = "\n".join(m["content"] for b in llm_log for m in b["messages"])
    assert f"Dana Example (chat): Here is the sheet: {LINK}" in prompt
    assert 'This is a Zoom meeting with the topic "Q4 budget review".' in prompt
    assert "Rafael Example" in prompt  # participants, also the silent one
    assert 'Lines marked "(chat)" were typed in the meeting chat' in prompt


def test_chat_during_processing_sends_the_session_back(pipeline, client):
    upload(pipeline)
    pipeline.end_session("m1")
    pipeline.store.set_session_status("m1", "processing")
    assert chat(client).json()["result"] == "queued"
    assert pipeline.store.get_session("m1")["status"] == "ended"
    pipeline.process_pending()
    segs = pipeline.store.session_segments("m1")
    assert pipeline.store.get_session("m1")["status"] == "done"
    assert sum(s.kind == CHAT for s in segs) == 2
    assert len({s.conversation_id for s in segs}) == 1


def test_reprocessing_keeps_chat_lines(done_meeting, pipeline):
    pipeline.settings.keep_audio = True  # nothing left to transcribe after the first pass
    pipeline.store.set_session_status("m1", "ended")
    pipeline.process_session("m1")
    chats = [s for s in pipeline.store.session_segments("m1") if s.kind == CHAT]
    assert [s.speaker for s in chats] == ["Dana Example", "Alice Example"]
    assert all(s.conversation_id for s in chats)


def test_reprocessing_with_audio_replaces_speech_but_keeps_chat(pipeline, client):
    pipeline.settings.keep_audio = True
    upload(pipeline)
    chat(client)
    process(pipeline)
    pipeline.store.set_session_status("m1", "ended")
    pipeline.process_session("m1")
    segs = pipeline.store.session_segments("m1")
    assert sum(s.kind == CHAT for s in segs) == 2
    assert sum(s.text == "Hello everyone, thanks for joining." for s in segs) == 1


def test_summary_prompt_marks_chat():
    segs = [Segment(T0, T0 + 2, "We need the numbers.", "Alice"),
            Segment(T0 + 4, T0 + 4, "see budget-v3.xlsx", "Dana", kind=CHAT)]
    assert format_transcript(segs, T0) == ("[0] Alice: We need the numbers.\n"
                                           "[4] Dana (chat): see budget-v3.xlsx")


# --------------------------------------------------------------- MCP 0.16.0


@pytest.fixture
def mcp(pipeline):
    return build_mcp(pipeline.store, pipeline.settings)


def test_mcp_version(mcp):
    import asyncio

    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            return client.server_info.version

    assert asyncio.run(go()) == "0.16.0"


def test_mcp_transcripts_show_chat(done_meeting, mcp):
    conv_id = done_meeting[0].conversation_id
    out = call(mcp, "get_transcript", conversation_id=conv_id)
    assert f"Dana Example (chat): Here is the sheet: {LINK}" in out
    assert "Alice Example: Hello everyone" in out
    hits = call(mcp, "search_transcripts", query="sheet")
    assert f'Dana Example (chat): "Here is the sheet: {LINK}"' in hits
    assert "(Zoom meeting: Q4 budget review)" in hits


def test_mcp_conversations_show_meeting_and_participants(done_meeting, mcp):
    conv_id = done_meeting[0].conversation_id
    out = call(mcp, "search_conversations")
    assert "(Zoom meeting: Q4 budget review)" in out
    assert ("participants: Alice Example, Bo Example, Me, Dana Example, Rafael Example"
            in out)
    found = call(mcp, "search_conversations", query="budget review")
    assert conv_id in found
    detail = call(mcp, "get_conversation", conversation_id=conv_id)
    assert "(Zoom meeting: Q4 budget review)" in detail
    assert "Rafael Example (" in detail  # joined–left times
    plain = Store(":memory:")
    assert plain.meetings_for([conv_id]) == {}


def test_meeting_without_topic(pipeline, client, mcp):
    upload(pipeline)
    segs = process(pipeline)
    out = call(mcp, "get_conversation", conversation_id=segs[0].conversation_id)
    assert "(Zoom meeting)" in out and "Participants: Alice Example, Bo Example, Me" in out


def test_json_rows_have_source(done_meeting, pipeline, mcp):
    conv_id = done_meeting[0].conversation_id
    # a recording that is no meeting
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0,
                          started_at=T0 + 86400, codec="wav", data=ALICE[2])
    process(pipeline, "s1")
    facts = json.loads(call(mcp, "list_facts", format="json"))["facts"]
    items = json.loads(call(mcp, "list_action_items", format="json"))
    for rows in (facts, items):
        assert {r["source"] for r in rows if r["conversation_id"] == conv_id} == {"meeting"}
        assert {r["source"] for r in rows if r["conversation_id"] != conv_id} == {None}
        assert any(r["conversation_id"] != conv_id for r in rows)
    text = call(mcp, "list_action_items")
    assert "(Zoom meeting: Q4 budget review)" in text


def test_view_page_shows_chat_and_meeting(done_meeting, client, pipeline):
    from convo_server.links import transcript_link

    conv_id = done_meeting[0].conversation_id
    link = transcript_link(pipeline.settings, conv_id).split("/view/")[1]
    page = client.get(f"/view/{link}").text
    assert "Zoom meeting: Q4 budget review" in page and "Rafael Example" in page
    assert "Dana Example <span class=via>(chat)</span>:" in page
    t = client.get(f"/v1/conversations/{conv_id}/transcript").json()
    assert {s["kind"] for s in t["segments"]} == {"speech", "chat"}
    conv = client.get(f"/v1/conversations/{conv_id}").json()
    assert conv["meeting"]["topic"] == "Q4 budget review"
    assert conv["meeting"]["participants"][0]["joined_at"].endswith("+00:00")


# ------------------------------------------- erase, retention, reassign_line


def test_erase_fact_on_a_chat_line(done_meeting, pipeline, client):
    store = pipeline.store
    conv_id = done_meeting[0].conversation_id
    with store.tx() as c:
        fid = c.execute("INSERT INTO facts (conversation_id, subject, fact, source_at)"
                        " VALUES (?, 'Dana Example', 'Dana shared budget-v3.xlsx', ?)",
                        (conv_id, T0 + 5)).lastrowid
    assert store.erase_fact(fid)["lines"] == 1
    line = next(s for s in store.session_segments("m1") if s.speaker == "Dana Example")
    assert line.text == "[forgotten]" and line.kind == CHAT
    assert not store.search_segments(query="sheet")
    # the bot's retry doesn't bring the words back
    assert chat(client).json()["duplicates"] == 2
    assert not any(LINK in s.text for s in store.session_segments("m1"))


def test_retention_removes_chat_lines_and_keeps_the_meeting(done_meeting, pipeline, mcp):
    conv_id = done_meeting[0].conversation_id
    out = pipeline.store.prune_transcripts(T0 + 10 ** 6)
    assert out["conversations"] == 1
    assert pipeline.store.session_segments("m1") == []
    assert "(Zoom meeting: Q4 budget review)" in call(mcp, "get_conversation",
                                                      conversation_id=conv_id)


def test_reassign_line_works_on_chat(done_meeting, pipeline, mcp):
    line = next(s for s in done_meeting if s.kind == CHAT and s.speaker == "Alice Example")
    out = call(mcp, "reassign_line", line_id=line.id, name="Bo Example")
    assert "moved from Alice Example to Bo Example" in out
    after = pipeline.store.get_segment(line.id)
    assert (after.speaker, after.kind) == ("Bo Example", CHAT)


# ---------------------------------------------------------- store and import


def test_old_database_gets_the_kind_column(tmp_path):
    db = tmp_path / "old.db"
    Store(db).close()
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE segments DROP COLUMN kind")
    con.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at, received_at)"
                " VALUES ('s', 'omi', 1, 1, 1)")
    con.execute("INSERT INTO segments (session_id, device, started_at, ended_at, speaker,"
                " text) VALUES ('s', 'omi', 1, 2, 'A', 'hi')")
    con.commit()
    con.close()
    store = Store(db)
    assert store.session_segments("s")[0].kind == SPEECH
    Store(db).close()  # a second open adds nothing


def test_meeting_chat_is_imported_only_on_a_chat_call(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        import convo_server.api, convo_server.cli, convo_server.mcp_server
        from fastapi.testclient import TestClient
        from convo_server.config import Settings
        from convo_server.pipeline import Pipeline
        from convo_server.store import Store
        from convo_server.vad import EnergyVAD
        s = Settings(ingest_token={TOKEN!r}, data_dir={str(tmp_path)!r}, run_worker=False,
                     vad_backend="energy")
        app = convo_server.api.create_app(Pipeline(s, Store(s.db_path), EnergyVAD()))
        with TestClient(app) as c:
            c.headers["Authorization"] = "Bearer {TOKEN}"
            assert c.get("/v1/sessions").status_code == 200
            assert "convo_server.meeting_chat" not in sys.modules, "loaded at startup"
            assert c.post("/v1/sessions/x/chat", json={{}}).status_code == 404
            assert "convo_server.meeting_chat" in sys.modules
        import threading
        # AnyIO's worker threads are told to stop when the loop closes but may take a
        # moment to exit; wait for them so only a thread left behind for good counts
        for t in threading.enumerate():
            if t.name == "AnyIO worker thread":
                t.join(timeout=10)
        print(sorted(t.name for t in threading.enumerate()))
    """)
    r = subprocess.run([sys.executable, "-c", script], cwd=SERVER, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['MainThread']"  # no thread or timer left behind


def test_seed_demo_budget_meeting_has_chat_and_participants(pipeline):
    sys.path.insert(0, str(SERVER / "scripts"))
    from seed_demo import MEETINGS, insert_meeting

    key, start, lines = next(m for m in MEETINGS if m[0] == "zoom-budget")
    (conv,) = insert_meeting(pipeline.store, pipeline, key, start, lines)
    segs = pipeline.store.conversation_segments(conv)
    chats = [(s.speaker, s.text) for s in segs if s.kind == CHAT]
    assert chats[0][0] == "Dana Example" and "garden-budget-v3.xlsx" in chats[0][1]
    assert len(segs) == len(lines) + 2 and segs == sorted(segs, key=lambda s: s.started_at)
    meeting = pipeline.store.meetings_for([conv])[conv]
    assert meeting["topic"] == "Q4 budget review: community garden"
    assert "Rafael Example" in [p["name"] for p in meeting["participants"]]
    assert pipeline.summarize_conversation(conv)
