"""QA regressions for lane 3B (meeting chat, topic and participants): Antigravity audit
BUG-02, BUG-12, BUG-13, BUG-14 and the server side of BUG-27."""

from __future__ import annotations

import pytest
from conftest import silence, wav
from test_mcp import call
from test_meeting_chat import chat, process, upload
from test_meeting_tracks import ALICE, T0, ScriptedWhisper

from convo_server.mcp_server import build_mcp
from convo_server.store import CHAT


@pytest.fixture
def mcp(pipeline):
    return build_mcp(pipeline.store, pipeline.settings)


SILENT = wav(silence(30))
CHAT_ONLY = {"topic": "Silent Sync",
             "participants": [{"name": "Alice Example", "zoom_user_id": ALICE[0]}],
             "lines": [{"at": T0 + 10, "from": "Alice Example", "zoom_user_id": ALICE[0],
                        "text": "Can everyone see my screen?"}]}


def silent_chunk(pipeline, session, track="", name=""):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="zoom", session_id=session, sequence=0, started_at=T0,
                          codec="wav", data=SILENT, track=track, track_name=name)


# ------------------------------------------------------------------ BUG-02
# Doesn't reproduce on this branch: process_session has no early return for a session
# without speech, and a meeting (tracks) or a Zoom session's chat lines are assigned by
# session, so queued chat always gets a conversation. These tests pin that. A chat call
# for a session with no chunk at all is 404 (the bot uploads audio first; a chunk that
# fails to decode creates no session either).


@pytest.mark.parametrize("track", ["", ALICE[0]])
def test_bug02_chat_only_meeting_becomes_a_conversation(pipeline, client, mcp, track):
    """Chunks with no speech at all (tracked or not), chat before /end: the chat lines get
    a conversation, the meeting is linked to it and it is summarized."""
    silent_chunk(pipeline, "zoom-silent", track, ALICE[1] if track else "")
    assert chat(client, CHAT_ONLY, session="zoom-silent").json()["result"] == "queued"
    pipeline.end_session("zoom-silent")
    pipeline.process_pending()
    assert pipeline.store.get_session("zoom-silent")["status"] == "done"
    segs = pipeline.store.session_segments("zoom-silent")
    assert [s.kind for s in segs] == [CHAT]
    conv_id = segs[0].conversation_id
    assert conv_id is not None
    assert pipeline.store.meeting_conversation("zoom-silent") == conv_id
    conv = pipeline.store.get_conversation(conv_id)
    assert conv["status"] == "summarized"
    assert conv["meeting"]["topic"] == "Silent Sync"
    assert "Can everyone see my screen?" in call(mcp, "get_transcript",
                                                 conversation_id=conv_id)
    assert conv_id in call(mcp, "search_conversations", query="Silent Sync")


def test_bug02_chat_after_a_silent_meeting_was_processed(pipeline, client):
    silent_chunk(pipeline, "zoom-silent", ALICE[0], ALICE[1])
    process(pipeline, "zoom-silent")
    assert pipeline.store.list_conversations() == []
    r = chat(client, CHAT_ONLY, session="zoom-silent").json()
    assert r["result"] == "attached" and r["conversation_id"]
    assert pipeline.store.meeting_conversation("zoom-silent") == r["conversation_id"]


def test_bug02_idle_finalize_picks_up_a_chat_only_meeting(pipeline, client):
    """The bot never calls /end: the idle timeout ends the session as for audio."""
    silent_chunk(pipeline, "zoom-silent", ALICE[0], ALICE[1])
    chat(client, CHAT_ONLY, session="zoom-silent")
    pipeline.finalize_idle(now=10**10)
    pipeline.process_pending()
    assert pipeline.store.meeting_conversation("zoom-silent") is not None


# ------------------------------------------------------------------ BUG-12


def test_bug12_one_zoom_user_is_one_person(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    body = {"participants": [{"name": "Dana Example (she/her)", "zoom_user_id": "16782336"}],
            "lines": [{"at": T0 + 5, "from": "Dana", "zoom_user_id": "16782336",
                       "text": "Hi!"}]}
    assert chat(client, body).status_code == 200
    names = [p["name"] for p in pipeline.store.people()]
    assert not ("Dana" in names and "Dana Example" in names), names
    segs = [s for s in pipeline.store.session_segments("m1") if s.kind == CHAT]
    parts = pipeline.store.get_conversation(
        process(pipeline)[0].conversation_id)["meeting"]["participants"]
    dana = [p for p in parts if p["name"].startswith("Dana")]
    assert len(dana) == 1 and dana[0]["name"] == segs[0].speaker
