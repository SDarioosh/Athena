"""Enroll a voice from the next recording, for the hub's "Enroll my voice"
(athena/voice_enroll.py)."""

from __future__ import annotations

import time

import httpx
import numpy as np
from fastapi.testclient import TestClient

from athena import voice_enroll
from athena.api import create_app
from athena.ingest_relay import add_ingest_routes
from athena.mcp_server import BearerAuth, build_mcp
from tests.conftest import TOKEN, ToneEmbedder, silence, tone, wav

MCP_TOKEN = "mcp-token-abcdefghijkl"


def record(pipeline, session, audio_parts, device="twatch-ultra", start=None):
    """Upload 30 s chunks of ``audio_parts`` and end the session."""
    start = time.time() if start is None else start
    for seq, part in enumerate(audio_parts):
        pipeline.ingest_chunk(device=device, session_id=session, sequence=seq,
                              started_at=start + 30 * seq, codec="wav", data=wav(part))
    pipeline.end_session(session)


def reading(seconds=45, freq=300):
    """``seconds`` of 'speech' at ``freq`` Hz, in 30 s chunks."""
    audio = np.concatenate([tone(freq, seconds), silence(30 - seconds % 30 or 30)])
    return [audio[i:i + 30 * 16000] for i in range(0, audio.size, 30 * 16000)]


def api(pipeline) -> TestClient:
    c = TestClient(create_app(pipeline))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_next_recording_becomes_the_owner_voice_and_no_conversation(pipeline):
    store = pipeline.store
    assert voice_enroll.status(pipeline)["state"] == "none"
    started = voice_enroll.start(pipeline, "Sam", now=time.time() - 1)
    assert started["state"] == "waiting" and started["name"] == "Sam"
    assert started["is_owner"] is True and started["recording"] is None

    record(pipeline, "enr", reading(45))
    rec = voice_enroll.status(pipeline)["recording"]
    assert rec["session_id"] == "enr" and rec["device"] == "twatch-ultra"
    assert rec["seconds"] == 60.0 and rec["status"] == "ended"

    assert pipeline.process_pending() == 1
    st = voice_enroll.status(pipeline)
    assert st["state"] == "done", st
    assert st["samples"] >= 3 and 44 <= st["speech_seconds"] <= 46
    assert st["session_id"] == "enr" and st["error"] is None
    # the owner is now Sam, with the recording's voice (300 Hz), and Me is no owner
    assert store.owner_name() == "Sam"
    sam = next(s for s in store.speakers() if s["name"] == "Sam")
    assert float(sam["embedding"] @ ToneEmbedder().embed(tone(300, 3))) > 0.99
    assert [v["source"] for v in store.voice_samples("Sam")] == ["enrolled"] * st["samples"]
    assert pipeline.diarizer.voices[0].name == "Sam"  # the worker uses it right away
    # read-aloud statements make no conversation; the audio is gone
    assert store.list_conversations() == []
    assert store.get_session("enr")["status"] == "done"
    assert all(c["audio_path"] is None for c in store.chunks("enr"))
    # the recording after it is an ordinary conversation again
    record(pipeline, "next", reading(10, 600))
    pipeline.process_pending()
    assert len(store.list_conversations()) == 1


def test_default_name_is_the_owner_and_named_samples_stay(pipeline):
    store = pipeline.store
    store.add_voice_sample("Me", ToneEmbedder().embed(tone(200, 3)), source="named")
    assert voice_enroll.start(pipeline, None, now=time.time() - 1)["name"] == "Me"
    record(pipeline, "enr", reading(40, 200))
    pipeline.process_pending()
    assert voice_enroll.status(pipeline)["state"] == "done"
    sources = [v["source"] for v in store.voice_samples("Me")]
    assert "named" in sources and "enrolled" in sources and store.owner_name() == "Me"


def test_a_recording_already_running_is_not_taken(pipeline):
    t0 = time.time()
    pipeline.ingest_chunk(device="twatch-ultra", session_id="before", sequence=0,
                          started_at=t0 - 30, codec="wav", data=wav(tone(600, 30)))
    voice_enroll.start(pipeline, "Sam", now=time.time())
    # its next chunk arrives after the start: still the recording from before
    pipeline.ingest_chunk(device="twatch-ultra", session_id="before", sequence=1,
                          started_at=t0, codec="wav", data=wav(tone(600, 30)))
    pipeline.end_session("before")
    assert voice_enroll.status(pipeline)["recording"] is None
    pipeline.process_pending()
    assert voice_enroll.status(pipeline)["state"] == "waiting"
    assert len(pipeline.store.list_conversations()) == 1


def test_meetings_and_the_zoom_bot_are_not_taken(pipeline):
    voice_enroll.start(pipeline, "Sam", now=time.time() - 1)
    record(pipeline, "z", reading(40), device="zoom")
    pipeline.ingest_chunk(device="omi", session_id="m", sequence=0, started_at=time.time(),
                          codec="wav", data=wav(tone(300, 30)), track="1",
                          track_name="Alex Example")
    assert voice_enroll.status(pipeline)["recording"] is None


def test_too_little_speech_fails_and_makes_no_conversation(pipeline):
    voice_enroll.start(pipeline, "Sam", now=time.time() - 1)
    record(pipeline, "short", reading(12))
    pipeline.process_pending()
    st = voice_enroll.status(pipeline)
    assert st["state"] == "failed" and "only 12 s of speech" in st["error"]
    assert pipeline.store.owner_name() == "Me"
    assert pipeline.store.list_conversations() == []
    assert pipeline.store.get_session("short")["status"] == "done"


def test_unexpected_error_keeps_the_audio(pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("athena.diarize.enroll_voice", boom)
    voice_enroll.start(pipeline, "Sam", now=time.time() - 1)
    record(pipeline, "enr", reading(40))
    pipeline.process_pending()
    st = voice_enroll.status(pipeline)
    assert st["state"] == "failed" and "disk on fire" in st["error"]
    assert pipeline.store.get_session("enr")["status"] == "failed"
    assert all(c["audio_path"] for c in pipeline.store.chunks("enr"))


def test_a_late_chunk_is_not_enrolled_again(pipeline):
    voice_enroll.start(pipeline, "Sam", now=time.time() - 1)
    t0 = time.time()
    record(pipeline, "enr", reading(40), start=t0)
    pipeline.process_pending()
    done = voice_enroll.status(pipeline)
    pipeline.ingest_chunk(device="twatch-ultra", session_id="enr", sequence=5,
                          started_at=t0 + 150, codec="wav", data=wav(tone(900, 30)))
    pipeline.end_session("enr")
    pipeline.process_pending()
    again = voice_enroll.status(pipeline)
    assert again["state"] == "done" and again["ended_at"] == done["ended_at"]
    assert pipeline.store.list_conversations() == []
    assert all(c["audio_path"] is None for c in pipeline.store.chunks("enr"))


def test_waiting_expires_and_cancel(pipeline):
    t = time.time()
    voice_enroll.start(pipeline, "Sam", now=t)
    assert voice_enroll.status(pipeline, now=t + voice_enroll.WAIT_SECONDS + 1)["state"] \
        == "expired"
    voice_enroll.start(pipeline, "Sam", now=t)
    assert voice_enroll.cancel(pipeline)["state"] == "cancelled"
    record(pipeline, "after", reading(10, 600))
    pipeline.process_pending()
    assert len(pipeline.store.list_conversations()) == 1
    assert voice_enroll.cancel(pipeline)["state"] == "cancelled"  # stays as it ended


def test_api(pipeline):
    c = api(pipeline)
    assert c.get("/v1/speakers/enroll/next").json()["state"] == "none"
    r = c.post("/v1/speakers/enroll/next", json={"name": "  Sam   Example "})
    assert r.status_code == 200 and r.json()["name"] == "Sam Example"
    body = c.get("/v1/speakers/enroll/next").json()
    assert body["state"] == "waiting" and body["default_name"] == "Me"
    assert body["owner_voice"] == "Me" and body["available"] is True
    assert c.post("/v1/speakers/enroll/next", json={"name": "x" * 101}).status_code == 400
    assert c.delete("/v1/speakers/enroll/next").json()["state"] == "cancelled"
    assert c.post("/v1/speakers/enroll/next", json={}).json()["name"] == "Me"
    pipeline.diarizer = None
    r = c.post("/v1/speakers/enroll/next", json={})
    assert r.status_code == 503 and "diarization" in r.json()["detail"]
    assert c.get("/v1/speakers/enroll/next").json()["available"] is False
    assert TestClient(create_app(pipeline)).get("/v1/speakers/enroll/next").status_code == 401


def test_processing_cannot_be_cancelled_or_replaced(pipeline):
    state = {"state": "processing", "name": "Sam", "is_owner": True,
             "started_at": time.time(), "expires_at": time.time() + 60, "session_id": "x"}
    pipeline.store.put_voice_enrollment(state)
    c = api(pipeline)
    assert c.delete("/v1/speakers/enroll/next").status_code == 409
    assert c.post("/v1/speakers/enroll/next", json={}).status_code == 409


def test_reachable_through_the_mcp_http_app(pipeline):
    mcp = build_mcp(pipeline.store, pipeline.settings)
    add_ingest_routes(mcp, pipeline.settings,
                      transport=httpx.ASGITransport(app=create_app(pipeline)))
    relay = TestClient(BearerAuth(mcp.streamable_http_app(), MCP_TOKEN))
    q = f"?access_token={MCP_TOKEN}"
    assert relay.get("/speakers/enroll/next").status_code == 401
    r = relay.post(f"/speakers/enroll/next{q}", json={"name": "Sam"})
    assert r.status_code == 200 and r.json()["state"] == "waiting"
    assert relay.get(f"/speakers/enroll/next{q}").json()["name"] == "Sam"
    assert relay.delete(f"/speakers/enroll/next{q}").json()["state"] == "cancelled"
    r = relay.post(f"/speakers/enroll/next{q}", json={"is_owner": "maybe"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
