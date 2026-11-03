"""Who is speaking in a short clip: POST /v1/speakers/identify and its relay route
(athena/speaker_identify.py). Fake embedder and VAD, fictional voices."""

from __future__ import annotations

import logging

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from athena.api import create_app
from athena.diarize import load_voices
from tests.conftest import TOKEN, ToneEmbedder, silence, tone, wav
from tests.unit.mcp.test_ingest_relay import MCP_TOKEN, mcp_client


@pytest.fixture
def api(pipeline) -> TestClient:
    # Alice (500 Hz) beside the owner "Me" (200 Hz, from the pipeline fixture)
    pipeline.store.put_speaker("Alice", ToneEmbedder().embed(tone(500, 3)), is_owner=False)
    pipeline.diarizer.voices = load_voices(pipeline.store)
    c = TestClient(create_app(pipeline))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def identify(c, data, **kw):
    return c.post("/v1/speakers/identify", files={"audio": ("cmd.wav", data, "audio/wav")},
                  **kw)


def _state(store):
    """Everything identify must leave as it is."""
    return ([(s["name"], s["is_owner"], s["samples"].shape) for s in store.speakers()],
            store.list_sessions(100), store.list_conversations())


def test_the_owner_is_recognised_and_nothing_is_written(api, pipeline):
    before = _state(pipeline.store)
    r = identify(api, wav(silence(0.5), tone(200, 2.5), silence(0.5)))
    assert r.status_code == 200, r.text
    body = r.json()
    owner_id = pipeline.store.owner_person_id(("Me",))
    assert body["speaker"] == {"id": None if owner_id is None else str(owner_id),
                               "name": "Me", "is_owner": True}
    assert body["score"] > 0.9
    assert body["threshold"] == pipeline.settings.speaker_match_threshold == 0.5
    assert 2.0 <= body["speech_seconds"] <= 3.0
    assert set(body) == {"speaker", "score", "threshold", "speech_seconds"}
    assert _state(pipeline.store) == before
    assert not list(pipeline.settings.data_dir.rglob("*.wav"))  # the audio isn't kept


def test_another_enrolled_speaker(api, pipeline):
    body = identify(api, wav(tone(500, 2))).json()
    assert body["speaker"]["name"] == "Alice" and body["speaker"]["is_owner"] is False
    assert body["speaker"]["id"] == str(pipeline.store.person_id("Alice"))


def test_an_unknown_voice_is_below_the_threshold(api):
    body = identify(api, wav(tone(900, 2))).json()
    assert body["speaker"] is None
    assert body["score"] is not None and body["score"] < body["threshold"]


def test_the_threshold_setting(api, pipeline):
    pipeline.settings.speaker_identify_threshold = 1.01
    body = identify(api, wav(tone(200, 2))).json()
    assert body["threshold"] == 1.01 and body["speaker"] is None and body["score"] > 0.9


def test_too_little_speech(api):
    body = identify(api, wav(silence(1), tone(200, 0.5), silence(1))).json()
    assert body == {"speaker": None, "score": None, "threshold": 0.5,
                    "speech_seconds": body["speech_seconds"]}
    assert body["speech_seconds"] < 1.0
    assert identify(api, wav(silence(3))).json()["speech_seconds"] == 0.0


def test_no_enrolled_voice(api, monkeypatch):
    from athena import speaker_identify

    monkeypatch.setattr(speaker_identify, "load_voices", lambda store: [])
    body = identify(api, wav(tone(200, 2))).json()
    assert body["speaker"] is None and body["score"] is None


def test_bad_audio_too_large_or_too_long(api, caplog):
    caplog.set_level(logging.DEBUG)
    r = identify(api, b"RIFF this is no audio at all")
    assert r.status_code == 422 and "could not decode" in r.json()["detail"]
    assert identify(api, b"").status_code in (400, 422)
    assert identify(api, b"\0" * (2 * 1024 * 1024 + 1)).status_code == 413
    r = identify(api, wav(tone(200, 21)))
    assert r.status_code == 413 and "longer than 20 s" in r.json()["detail"]
    assert identify(api, wav(tone(200, 20))).status_code == 200


def test_needs_the_ingest_token(api):
    data = wav(tone(200, 2))
    assert identify(api, data, headers={"Authorization": "Bearer nope"}).status_code == 401
    del api.headers["Authorization"]
    assert identify(api, data).status_code == 401


def test_diarization_off(api, pipeline):
    pipeline.diarizer = None
    assert identify(api, wav(tone(200, 2))).status_code == 503


def test_relay_forwards_unchanged(api, pipeline):
    relay = mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline)))
    data = wav(silence(0.5), tone(200, 2))
    files = {"audio": ("cmd.wav", data, "audio/wav")}
    assert relay.post("/speakers/identify", files=files).status_code == 401
    r = relay.post(f"/speakers/identify?access_token={MCP_TOKEN}", files=files)
    assert r.status_code == 200, r.text
    assert r.json() == identify(api, data).json()
    assert r.json()["speaker"]["name"] == "Me"
    auth = {"Authorization": f"Bearer {MCP_TOKEN}"}
    # the ingest token is no MCP token
    assert relay.post("/speakers/identify", files=files,
                      headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    r = relay.post("/speakers/identify", content=b"{}",
                   headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 415
    big = {"audio": ("cmd.wav", b"\0" * (2 * 1024 * 1024 + 70 * 1024), "audio/wav")}
    assert relay.post("/speakers/identify", files=big, headers=auth).status_code == 413
    bad = relay.post("/speakers/identify", headers=auth,
                     files={"audio": ("x.wav", b"no audio", "audio/wav")})
    assert bad.status_code == 422 and isinstance(bad.json()["detail"], str)


def test_embedding_is_of_the_speech_only(pipeline):
    """Silence around the command doesn't change who it is."""
    from athena import speaker_identify

    a = speaker_identify.identify(pipeline, wav(silence(5), tone(200, 2), silence(5)))
    b = speaker_identify.identify(pipeline, wav(tone(200, 2)))
    assert a["speaker"] == b["speaker"] and np.isclose(a["score"], b["score"])
