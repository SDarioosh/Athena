"""QA regressions for lane 3B (meeting chat, topic and participants): Antigravity audit
BUG-02, BUG-12, BUG-13, BUG-14 and the server side of BUG-27."""

from __future__ import annotations

import json

import pytest
from conftest import silence, wav
from test_mcp import call
from test_meeting_chat import chat, process, upload
from test_meeting_tracks import ALICE, BO, T0, ScriptedWhisper

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


# ------------------------------------------------------------------ BUG-13


def test_bug13_two_people_with_one_name_are_both_listed(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    body = {"participants": [
        {"name": "Alex", "zoom_user_id": "16790000", "joined_at": T0},
        {"name": "Alex", "zoom_user_id": "16790001", "joined_at": T0 + 600}]}
    chat(client, body)
    conv_id = process(pipeline)[0].conversation_id
    parts = pipeline.store.get_conversation(conv_id)["meeting"]["participants"]
    assert [p["joined_at"] for p in parts if p["name"] == "Alex"] == [T0, T0 + 600]


def test_bug13_track_speaker_and_participant_with_one_zoom_id_listed_once(pipeline, client):
    """Zoom shows a device name ("iPhone"): the track's lines are "Speaker 1", the
    participant list says "iPhone" for the same zoom_user_id."""
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2], track=ALICE[0], track_name="iPhone")
    chat(client, {"participants": [{"name": "iPhone", "zoom_user_id": ALICE[0]}]})
    segs = process(pipeline)
    speakers = {s.speaker for s in segs}
    parts = pipeline.store.get_conversation(segs[0].conversation_id)["meeting"]["participants"]
    # the device name is not listed beside the speakers heard on its track; they keep it
    # as their Zoom name
    assert sorted(p["name"] for p in parts) == sorted(speakers), parts
    assert {p["zoom_name"] for p in parts} == {"iPhone"}


def test_bug13_device_participant_never_heard_stays_listed(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    chat(client, {"participants": [{"name": ALICE[1], "zoom_user_id": ALICE[0]},
                                   {"name": "iPhone", "zoom_user_id": "16790002"}]})
    conv_id = process(pipeline)[0].conversation_id
    parts = pipeline.store.get_conversation(conv_id)["meeting"]["participants"]
    assert [p["name"] for p in parts] == [ALICE[1], "iPhone"]


def test_bug13_resent_participant_listed_once(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    body = {"participants": [{"name": "Rafael Example", "zoom_user_id": "16790003"},
                             {"name": "Rafael Example (mobile)", "zoom_user_id": "16790003"}]}
    chat(client, body)
    conv_id = process(pipeline)[0].conversation_id
    parts = pipeline.store.get_conversation(conv_id)["meeting"]["participants"]
    assert [p["name"] for p in parts].count("Rafael Example") == 1


# ------------------------------------------------------------------ BUG-14


def test_bug14_late_topic_refreshes_search(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    conv_id = process(pipeline)[0].conversation_id
    r = chat(client, {"topic": "Project Phoenix Retro", "participants": [], "lines": []})
    assert r.json()["result"] == "attached"
    assert conv_id in {h["conversation_id"] for h in pipeline.store.fts_search("Phoenix")}


def test_bug14_late_participants_refresh_search(pipeline, client):
    upload(pipeline, tracks=(ALICE,))
    conv_id = process(pipeline)[0].conversation_id
    chat(client, {"participants": [{"name": "Rafael Quintero", "zoom_user_id": "16799999"}]})
    assert conv_id in {h["conversation_id"] for h in pipeline.store.fts_search("Quintero")}


# ------------------------------------------------------------------ BUG-27


def conv_json(client, conv_id):
    r = client.get(f"/v1/conversations/{conv_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_bug27_meeting_without_chat_call_has_a_meeting_block(pipeline, client, mcp):
    upload(pipeline, tracks=(ALICE, BO))
    conv_id = process(pipeline)[0].conversation_id
    m = conv_json(client, conv_id)["meeting"]
    assert m["topic"] is None
    assert [p["name"] for p in m["participants"]] == [ALICE[1], BO[1]]
    assert "(Online meeting)" in call(mcp, "get_conversation", conversation_id=conv_id)


def test_bug27_untracked_zoom_session_without_chat_call(pipeline, client, mcp):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="zoom", session_id="z1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2])
    conv_id = process(pipeline, "z1")[0].conversation_id
    m = conv_json(client, conv_id).get("meeting")
    assert m is not None and m["topic"] is None and m["participants"]
    assert "(Online meeting)" in call(mcp, "get_conversation", conversation_id=conv_id)
    facts = json.loads(call(mcp, "list_facts", format="json"))["facts"]
    assert {f["source"] for f in facts if f["conversation_id"] == conv_id} <= {"meeting"}


def test_bug27_tracked_non_zoom_session_is_a_meeting(pipeline, client):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="bot-test", session_id="t1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2], track=ALICE[0], track_name=ALICE[1])
    assert chat(client, {"topic": "Test"}, session="t1").status_code == 200
    conv_id = process(pipeline, "t1")[0].conversation_id
    assert conv_json(client, conv_id)["meeting"]["topic"] == "Test"


def test_bug27_plain_recording_has_no_meeting_block(pipeline, client):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0, started_at=T0,
                          codec="wav", data=ALICE[2])
    conv_id = process(pipeline, "s1")[0].conversation_id
    assert conv_json(client, conv_id).get("meeting") is None
