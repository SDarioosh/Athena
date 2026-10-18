"""Ingestion API: auth, validation, silence/max-duration stop, idempotent retries."""

from __future__ import annotations

from conftest import TOKEN, silence, tone, wav

T0 = 1_760_000_000.0


def upload(client, seq, data, session="s1", device="trex3pro", ts=None, **headers):
    return client.post(
        "/v1/ingest/chunk",
        files={"audio": ("c.wav", data, "audio/wav")},
        data={"device": device, "session_id": session, "sequence": str(seq),
              "timestamp": str(ts if ts is not None else T0 + 30 * seq), "codec": "wav"},
        headers=headers or None,
    )


def test_health_is_public(client):
    client.headers.pop("Authorization")
    assert client.get("/health").json() == {"ok": True}


def test_requires_bearer_token(client):
    client.headers.pop("Authorization")
    assert upload(client, 0, wav(silence(1))).status_code == 401
    assert upload(client, 0, wav(silence(1)), Authorization="Bearer nope").status_code == 401
    assert upload(client, 0, wav(silence(1)), Authorization=TOKEN).status_code == 401
    assert client.get("/v1/conversations").status_code == 401
    assert upload(client, 0, wav(silence(1)),
                  Authorization=f"Bearer {TOKEN}").status_code == 200


def test_validation_errors(client):
    assert upload(client, 0, wav(silence(1)), session="../etc").status_code == 400
    assert upload(client, 0, wav(silence(1)), ts="yesterday").status_code == 400
    r = client.post("/v1/ingest/chunk", files={"audio": ("c", b"x" * 10)},
                    data={"device": "d", "session_id": "s", "sequence": "0",
                          "timestamp": "2026-01-01T00:00:00Z", "codec": "flac2"})
    assert r.status_code == 400


def test_silence_triggers_stop(client):
    # threshold is 60 s in the test settings; chunks are 30 s
    r = upload(client, 0, wav(tone(200, 5), silence(25)))
    assert r.json()["stop"] is False and r.json()["speech_seconds"] > 4
    r = upload(client, 1, wav(silence(30)))
    assert r.json()["stop"] is False
    assert 54 < r.json()["trailing_silence_seconds"] < 56
    r = upload(client, 2, wav(silence(30)))
    body = r.json()
    assert body["stop"] is True and body["reason"] == "silence"
    assert client.get("/v1/sessions/s1").json()["status"] == "ended"
    # a straggler after stop is still accepted but told to stop
    assert upload(client, 3, wav(silence(30))).json()["stop"] is True


def test_speech_resets_silence(client):
    upload(client, 0, wav(silence(30)))
    upload(client, 1, wav(silence(25), tone(200, 5)))
    assert upload(client, 2, wav(silence(30))).json()["stop"] is False


def test_out_of_order_chunks(client):
    upload(client, 2, wav(silence(30)))
    r = upload(client, 1, wav(tone(200, 30)))
    assert r.json()["stop"] is False
    sess = client.get("/v1/sessions/s1").json()
    assert [c["sequence"] for c in sess["chunks"]] == [1, 2]


def test_max_duration_stop(client, settings):
    settings.silence_stop_seconds = 10_000
    for seq in range(19):
        assert upload(client, seq, wav(tone(200, 30))).json()["stop"] is False
    r = upload(client, 19, wav(tone(200, 30)))
    assert r.json() | {"stop": True, "reason": "max_duration"} == r.json()


def test_duplicate_chunk_is_idempotent(client):
    assert upload(client, 0, wav(tone(200, 30))).json()["duplicate"] is False
    r = upload(client, 0, wav(tone(200, 30)))
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert len(client.get("/v1/sessions/s1").json()["chunks"]) == 1


def test_session_cannot_switch_device(client):
    upload(client, 0, wav(silence(1)))
    assert upload(client, 1, wav(silence(1)), device="omi").status_code == 400


def test_end_session(client):
    assert client.post("/v1/sessions/nope/end").status_code == 404
    upload(client, 0, wav(silence(1)))
    assert client.post("/v1/sessions/s1/end").json()["stop"] is True
    assert client.get("/v1/sessions/s1").json()["stop_reason"] == "client"


def test_iso_and_epoch_ms_timestamps(client):
    assert upload(client, 0, wav(silence(1)), ts="2026-10-18T10:00:00Z").status_code == 200
    assert upload(client, 0, wav(silence(1)), session="s2",
                  ts=str(int(T0 * 1000))).status_code == 200
    started = client.get("/v1/sessions/s2").json()["started_at"]
    assert started.startswith("2025-10-09")


def test_json_ingest_for_zepp_side_service(client):
    import base64

    body = {"device": "trex3pro", "session_id": "j1", "sequence": 0, "timestamp": str(T0),
            "codec": "wav", "audio_b64": base64.b64encode(wav(tone(200, 5), silence(25))).decode()}
    r = client.post("/v1/ingest/chunk.json", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["stop"] is False and r.json()["speech_seconds"] > 4
    assert client.post("/v1/ingest/chunk.json", json=body).json()["duplicate"] is True
    assert client.post("/v1/ingest/chunk.json",
                       json={**body, "audio_b64": "%%%"}).status_code == 400
    client.headers.pop("Authorization")
    assert client.post("/v1/ingest/chunk.json", json=body).status_code == 401


def test_concurrent_retry_of_same_chunk_keeps_audio(pipeline, settings):
    """A retry racing the original upload must not delete the original's temp file."""
    real_add = pipeline.store.add_chunk
    data = wav(tone(200, 30))
    retried = []

    def add_then_retry(**kw):
        sess = real_add(**kw)  # original upload has committed its row...
        if not retried:  # ...and the device's retry arrives before the file is renamed
            retried.append(pipeline.ingest_chunk(device="trex3pro", session_id="s1", sequence=0,
                                                 started_at=T0, codec="wav", data=data))
        return sess

    pipeline.store.add_chunk = add_then_retry
    first = pipeline.ingest_chunk(device="trex3pro", session_id="s1", sequence=0,
                                  started_at=T0, codec="wav", data=data)
    assert first.duplicate is False and retried[0].duplicate is True
    chunk_dir = settings.audio_dir / "trex3pro" / "s1"
    assert (chunk_dir / "000000.wav").read_bytes() == data
    assert not list(chunk_dir.glob("*.part"))


def test_undecodable_upload_is_kept_for_debugging(client, settings):
    r = client.post("/v1/ingest/chunk", files={"audio": ("c", b"not audio at all")},
                    data={"device": "trex3pro", "session_id": "trex-1", "sequence": "3",
                          "timestamp": str(T0), "codec": "opus"})
    assert r.status_code == 422
    saved = list((settings.data_dir / "rejected").glob("*.bin"))
    assert [p.name for p in saved] == ["trex-1-3.opus.bin"]
    assert saved[0].read_bytes() == b"not audio at all"
