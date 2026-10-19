"""Voice notes: kind=note sessions are the owner's, one conversation, never joined or
discarded, and summarized with the note prompts."""

from __future__ import annotations

import base64
import sqlite3

import numpy as np
from conftest import fake_llm_transport, silence, tone, wav

from convo_server.llm import LLMClient, Summarizer
from convo_server.store import Segment, Store
from convo_server.transcribe import TranscriptPiece

T0 = 1_760_000_000.0


def upload(client, seq, data, session="n1", kind=None, ts=None):
    fields = {"device": "twatch", "session_id": session, "sequence": str(seq),
              "timestamp": str(ts if ts is not None else T0 + 30 * seq), "codec": "wav"}
    if kind is not None:
        fields["kind"] = kind
    return client.post("/v1/ingest/chunk", files={"audio": ("c.wav", data, "audio/wav")},
                       data=fields)


def ingest(pipeline, seq, audio, session, start, kind="conversation"):
    return pipeline.ingest_chunk(device="omi", session_id=session, sequence=seq,
                                 started_at=start, codec="wav", data=wav(audio), kind=kind)


def record(pipeline, session, start, kind="conversation", freq=200):
    ingest(pipeline, 0, np.concatenate([tone(freq, 5), silence(25)]), session, start, kind)
    pipeline.end_session(session)
    pipeline.process_pending()


def conv_of(pipeline, session):
    (conv_id,) = pipeline.store.session_conversations(session)
    return pipeline.store.get_conversation(conv_id)


# ------------------------------------------------------------------ ingest
def test_kind_defaults_to_conversation(client):
    assert upload(client, 0, wav(silence(1)), session="c1").status_code == 200
    assert client.get("/v1/sessions/c1").json()["kind"] == "conversation"


def test_note_kind_is_stored_and_retries_stay_idempotent(client):
    data = wav(tone(200, 5), silence(25))
    assert upload(client, 0, data, kind="note").json()["duplicate"] is False
    r = upload(client, 0, data, kind="note")
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert upload(client, 1, wav(silence(30)), kind="note").status_code == 200
    assert client.get("/v1/sessions/n1").json()["kind"] == "note"


def test_invalid_kind_is_400(client):
    r = upload(client, 0, wav(silence(1)), kind="memo")
    assert r.status_code == 400 and "kind" in r.json()["detail"]
    assert client.get("/v1/sessions/n1").status_code == 404


def test_kind_must_match_the_session(client):
    upload(client, 0, wav(silence(1)), kind="note")
    r = upload(client, 1, wav(silence(1)))  # omitted = conversation
    assert r.status_code == 400 and "note" in r.json()["detail"]
    upload(client, 0, wav(silence(1)), session="c1")
    assert upload(client, 1, wav(silence(1)), session="c1", kind="note").status_code == 400
    assert len(client.get("/v1/sessions/n1").json()["chunks"]) == 1


def test_json_ingest_takes_kind(client):
    body = {"device": "trex3pro", "session_id": "j1", "sequence": 0, "timestamp": str(T0),
            "codec": "wav", "kind": "note",
            "audio_b64": base64.b64encode(wav(tone(200, 5), silence(25))).decode()}
    assert client.post("/v1/ingest/chunk.json", json=body).status_code == 200
    assert client.get("/v1/sessions/j1").json()["kind"] == "note"
    bad = {**body, "session_id": "j2", "kind": "bogus"}
    assert client.post("/v1/ingest/chunk.json", json=bad).status_code == 400


def test_migration_gives_old_sessions_kind_conversation(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, device TEXT NOT NULL,"
        " started_at REAL NOT NULL, last_chunk_at REAL NOT NULL, last_speech_at REAL,"
        " received_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'recording',"
        " stop_reason TEXT, error TEXT);"
        "INSERT INTO sessions (id, device, started_at, last_chunk_at, received_at)"
        " VALUES ('old', 'omi', 1, 2, 3);"
        "PRAGMA user_version = 4;")
    old.close()
    store = Store(path)
    assert store.get_session("old")["kind"] == "conversation"
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5


# ---------------------------------------------------------------- pipeline
def test_note_is_one_owner_conversation_without_diarizer(pipeline, llm_log, monkeypatch):
    def no_diarizer(*a, **kw):
        raise AssertionError("the diarizer must not run on a voice note")

    monkeypatch.setattr(pipeline.diarizer, "diarize_runs", no_diarizer)
    # a 600 Hz voice (not the owner's) and a 5 min pause: still one owner note
    ingest(pipeline, 0, np.concatenate([tone(600, 5), silence(25)]), "n1", T0, "note")
    for seq in range(1, 11):
        ingest(pipeline, seq, silence(30), "n1", T0 + 30 * seq, "note")
    ingest(pipeline, 11, np.concatenate([tone(600, 5), silence(25)]), "n1", T0 + 330, "note")
    pipeline.end_session("n1")
    pipeline.process_pending()

    convs = pipeline.store.list_conversations()
    assert len(convs) == 1
    conv = pipeline.store.get_conversation(convs[0]["id"])
    assert conv["kind"] == "note" and conv["status"] == "summarized"
    segs = pipeline.store.conversation_segments(conv["id"])
    assert [(s.speaker, s.is_owner) for s in segs] == [("Me", True), ("Me", True)]

    names = [b["response_format"]["json_schema"]["name"] for b in llm_log]
    assert "Triage" not in names
    systems = [b["messages"][0]["content"] for b in llm_log]
    assert any("voice note" in s and "the idea in one or two" in s for s in systems)
    assert any("to-dos in a voice note" in s for s in systems)


def test_note_owner_name_falls_back_to_settings(pipeline):
    pipeline.store._conn.execute("DELETE FROM speakers")
    pipeline.settings.owner_name = "Sam"
    record(pipeline, "n1", T0, "note", freq=600)
    segs = pipeline.store.session_segments("n1")
    assert [(s.speaker, s.is_owner) for s in segs] == [("Sam", True)]


def test_note_is_never_joined_with_nearby_conversations(pipeline):
    record(pipeline, "c1", T0)                    # a conversation
    record(pipeline, "n1", T0 + 60, "note")       # a note 1 min later: within the gap
    record(pipeline, "c2", T0 + 120)              # the conversation goes on afterwards
    store = pipeline.store
    assert len(store.list_conversations()) == 2
    note = conv_of(pipeline, "n1")
    assert note["kind"] == "note"
    assert [s.session_id for s in store.conversation_segments(note["id"])] == ["n1"]
    # the two conversation halves join each other across the note, as before
    assert conv_of(pipeline, "c1")["id"] == conv_of(pipeline, "c2")["id"] != note["id"]
    assert conv_of(pipeline, "c1")["kind"] == "casual"


def test_late_chunk_keeps_one_note_conversation(pipeline):
    record(pipeline, "n1", T0, "note")
    first = conv_of(pipeline, "n1")["id"]
    # the relay drains a late chunk after the note was processed, 5 min later
    ingest(pipeline, 1, np.concatenate([tone(600, 5), silence(25)]), "n1", T0 + 300, "note")
    pipeline.end_session("n1")
    pipeline.process_pending()
    convs = pipeline.store.list_conversations()
    assert [c["id"] for c in convs] == [first]
    assert len(pipeline.store.conversation_segments(first)) == 2
    assert pipeline.store.get_conversation(first)["kind"] == "note"


def test_one_word_note_is_summarized(pipeline):
    pipeline.transcriber.transcribe = lambda audio, prompt="": [
        TranscriptPiece(0.0, 1.0, "Milk.")]
    record(pipeline, "n1", T0, "note")
    conv = conv_of(pipeline, "n1")
    assert conv["status"] == "summarized" and conv["kind"] == "note"
    assert [a["text"] for a in conv["action_items"]] == ["Book flights"]

    # the same word as an ordinary conversation is discarded as too short
    record(pipeline, "c1", T0 + 3600)
    assert conv_of(pipeline, "c1")["status"] == "discarded"


def test_summarizer_note_skips_length_check_and_triage():
    log: list = []
    llm = LLMClient("http://x/v1", "m", transport=fake_llm_transport(
        log=log, triage={"discard": True, "reason": "gibberish", "kind": "other"}))
    out = Summarizer(llm, "Me").summarize([Segment(0, 1, "Milk", "Me", True)], kind="note")
    assert out["kind"] == "note" and not out.get("discard")
    assert "Triage" not in [b["response_format"]["json_schema"]["name"] for b in log]
    # any other stored kind still goes through the length check and triage
    out = Summarizer(llm, "Me").summarize([Segment(0, 1, "Milk", "Me", True)], kind="casual")
    assert out["discard"] is True


# --------------------------------------------------------------------- MCP
def test_mcp_marks_voice_notes(pipeline):
    from test_mcp import call

    from convo_server.mcp_server import build_mcp

    record(pipeline, "c1", T0)
    record(pipeline, "n1", T0 + 60, "note")
    mcp = build_mcp(pipeline.store, pipeline.settings)
    note = conv_of(pipeline, "n1")
    listing = call(mcp, "search_conversations", since="2025-10-09", until="2025-10-09")
    assert listing.count("(voice note)") == 1
    assert f"\"Planning the trip\" (voice note) id {note['id']}" in listing
    detail = call(mcp, "get_conversation", conversation_id=note["id"])
    assert "(voice note)" in detail and "type note" in detail
    items = call(mcp, "list_action_items")
    assert items.count("(voice note)") == 1
    lines = call(mcp, "search_transcripts", query="words")
    assert lines.count("(voice note)") == 1
