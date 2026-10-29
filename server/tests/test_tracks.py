"""Chunks per track (POST /v1/ingest/chunk ``track``/``track_name``): one participant's
audio in a session with several, e.g. a Zoom meeting. Fictional data only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import TOKEN, silence, tone, wav
from starlette.testclient import TestClient

from athena.api import create_app
from athena.db.sessions import DuplicateChunk
from athena.db.store import Store
from athena.ingest_relay import add_ingest_routes
from athena.mcp_server import BearerAuth, build_mcp
from athena.pipeline import chunk_filename, validate_track

T0 = 1_760_000_000.0


def upload(client, seq, data, session="m1", device="zoom", ts=None, path="/v1/ingest/chunk",
           **fields):
    form = {"device": device, "session_id": session, "sequence": str(seq),
            "timestamp": str(ts if ts is not None else T0 + 30 * seq), "codec": "wav",
            **{k: v for k, v in fields.items() if v is not None}}
    return client.post(path, data=form, files={"audio": ("c.wav", data, "audio/wav")})


# ------------------------------------------------------------ without a track


def test_no_track_is_exactly_as_before(client, pipeline, settings):
    r = upload(client, 0, wav(tone(200, 5), silence(25)), session="s1", device="trex3pro")
    assert r.status_code == 200
    body = r.json()
    assert "track" not in body  # same answer as before tracks
    assert set(body) == {"ok", "stop", "reason", "duplicate", "speech_seconds",
                         "trailing_silence_seconds", "session_seconds", "session_id",
                         "sequence"}
    # same file name as before tracks
    assert (settings.audio_dir / "trex3pro" / "s1" / "000000.wav").exists()
    (row,) = pipeline.store.chunks("s1")
    assert row["track"] == "" and row["track_name"] is None
    assert pipeline.store.tracks("s1") == []
    # an empty track field is no track
    r = upload(client, 1, wav(silence(30)), session="s1", device="trex3pro", track="")
    assert r.status_code == 200 and "track" not in r.json()
    assert [c["track"] for c in pipeline.store.chunks("s1")] == ["", ""]
    sess = client.get("/v1/sessions/s1").json()
    assert sess["tracks"] == [] and [c["track"] for c in sess["chunks"]] == [None, None]
    # a retry is still a no-op, and the next sequence still counts per session
    assert upload(client, 1, wav(silence(30)), session="s1",
                  device="trex3pro").json()["duplicate"] is True


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("track", ["a b", "a/b", "x" * 65, "é", "a\nb"])
def test_bad_track_is_400(client, track):
    r = upload(client, 0, wav(silence(1)), track=track)
    assert r.status_code == 400
    assert r.json()["detail"] == "track must match ^[A-Za-z0-9._:-]{1,64}$"


def test_track_name_rules(client, pipeline):
    r = upload(client, 0, wav(silence(1)), track="16778240", track_name="x" * 201)
    assert r.status_code == 400
    assert r.json()["detail"] == "track_name is longer than 200 characters"
    r = upload(client, 0, wav(silence(1)), track_name="Alice Example")
    assert r.status_code == 400 and r.json()["detail"] == "track_name needs a track"
    # control characters dropped and spaces collapsed; 200 characters is fine
    r = upload(client, 0, wav(silence(1)), track="16778240",
               track_name="  Alice\x00 \t Example ")
    assert r.status_code == 200 and r.json()["track"] == "16778240"
    assert pipeline.store.chunks("m1")[0]["track_name"] == "Alice Example"
    assert upload(client, 1, wav(silence(1)), track="16778240",
                  track_name="y" * 200).status_code == 200
    # an empty name is no name, with or without a track
    assert upload(client, 2, wav(silence(1)), track="16778240",
                  track_name=" ").status_code == 200
    assert upload(client, 0, wav(silence(1)), session="s2", device="trex3pro",
                  track_name="").status_code == 200


def test_validate_track_values():
    assert validate_track(None, None) == ("", None)
    assert validate_track("", "") == ("", None)
    assert validate_track("zoom:16778240", "Bo") == ("zoom:16778240", "Bo")
    assert validate_track("..", None) == ("..", None)  # valid, and its file name is safe
    assert validate_track("a" * 64, None)[0] == "a" * 64


def test_chunk_file_names_are_safe_and_distinct():
    assert chunk_filename("", 3, "opus") == "000003.opus"
    names = {chunk_filename(t, 0, "opus") for t in ("..", ".", "a:b", "a_b", "A", "a")}
    assert len(names) == 6
    for n in names:
        assert "/" not in n and ":" not in n and not n.startswith(".")
        assert len(n) < 255
    assert len(chunk_filename("x" * 64, 999_999, "opus")) < 255


def test_json_route_accepts_tracks(client, pipeline):
    import base64

    body = {"device": "zoom", "session_id": "j1", "sequence": 0,
            "timestamp": str(T0), "codec": "wav", "track": "16778240",
            "track_name": "Alice Example",
            "audio_b64": base64.b64encode(wav(silence(1))).decode()}
    r = client.post("/v1/ingest/chunk.json", json=body)
    assert r.status_code == 200 and r.json()["track"] == "16778240"
    assert pipeline.store.tracks("j1")[0]["name"] == "Alice Example"
    bad = {**body, "sequence": 1, "track": "a b"}
    assert client.post("/v1/ingest/chunk.json", json=bad).status_code == 400
    untracked = {k: v for k, v in body.items() if k not in ("track", "track_name")}
    r = client.post("/v1/ingest/chunk.json", json={**untracked, "session_id": "j2"})
    assert r.status_code == 200 and "track" not in r.json()


# ------------------------------------------------------- storing and retrying


def test_retried_chunk_is_a_no_op_per_track(client, pipeline, settings):
    first = wav(tone(300, 30))
    assert upload(client, 0, first, track="16778240",
                  track_name="Alice Example").json()["duplicate"] is False
    again = upload(client, 0, wav(silence(30)), track="16778240", track_name="Someone Else")
    assert again.status_code == 200 and again.json()["duplicate"] is True
    # the same sequence on another track is another chunk
    other = upload(client, 0, wav(silence(30)), track="16779264", track_name="Bo Sample")
    assert other.json()["duplicate"] is False
    rows = pipeline.store.chunks("m1")
    assert [(c["track"], c["sequence"]) for c in rows] == [("16778240", 0), ("16779264", 0)]
    # the retry changed nothing: original audio and name kept
    assert Path(rows[0]["audio_path"]).read_bytes() == first
    assert rows[0]["track_name"] == "Alice Example"
    with pytest.raises(DuplicateChunk):
        pipeline.store.add_chunk(session_id="m1", device="zoom", sequence=0, started_at=T0,
                                 duration=30, codec="wav", audio_path=None, speech=[],
                                 track="16779264")


def test_track_names_keep_history(client, pipeline):
    for seq, name in enumerate(["Alice", "Alice (phone)", None, "Alice"]):
        upload(client, seq, wav(silence(30)), track="16778240", track_name=name)
    upload(client, 0, wav(silence(30)), ts=T0 + 45, track="16779264", track_name="Bo")
    tracks = pipeline.store.tracks("m1")
    assert [t["track"] for t in tracks] == ["16778240", "16779264"]  # by first chunk time
    alice, bo = tracks
    assert alice["name"] == "Alice"  # latest chunk with a name
    assert alice["names"] == ["Alice", "Alice (phone)"]
    assert alice["chunks"] == 4
    assert alice["started_at"] == T0 and alice["ended_at"] == pytest.approx(T0 + 120)
    assert bo == {"track": "16779264", "name": "Bo", "names": ["Bo"], "chunks": 1,
                  "started_at": T0 + 45, "ended_at": pytest.approx(T0 + 75)}
    sess = client.get("/v1/sessions/m1").json()
    assert [t["name"] for t in sess["tracks"]] == ["Alice", "Bo"]
    assert sess["tracks"][0]["started_at"].startswith("2025-10-09T")
    assert [(c["track"], c["sequence"], c["track_name"]) for c in sess["chunks"]][:2] == [
        ("16778240", 0, "Alice"), ("16778240", 1, "Alice (phone)")]
    assert pipeline.store.chunks("m1", track="16779264")[0]["track_name"] == "Bo"


# ---------------------------------------------- overlapping tracks kept apart


def test_two_overlapping_tracks_are_kept_apart(client, pipeline, settings):
    # Alice (300 Hz) and Bo (500 Hz) talk at the same time on their own tracks.
    for seq in range(2):
        assert upload(client, seq, wav(tone(300, 10), silence(20)), track="16778240",
                      track_name="Alice").json()["ok"]
        assert upload(client, seq, wav(tone(500, 10), silence(20)), track="16779264",
                      track_name="Bo").json()["ok"]
    files = sorted(p.name for p in (settings.audio_dir / "zoom" / "m1").iterdir())
    assert len(files) == 4 and all(f.startswith("t") for f in files)

    runs = list(pipeline._track_runs(pipeline.store.chunks("m1")))
    # each track's two chunks are contiguous by its own sequence: one run per track
    assert [(t, start) for t, start, _, _ in runs] == [("16778240", T0), ("16779264", T0)]
    assert all(audio.size == 60 * 16000 for _, _, audio, _ in runs)

    pipeline.end_session("m1")
    pipeline.process_session("m1")
    segs = pipeline.store.session_segments("m1")
    texts = sorted(s.text for s in segs)
    # one line per speech region per track; a mix would hear one dominant tone only
    assert texts == ["words at 300 hz"] * 2 + ["words at 500 hz"] * 2
    assert sorted(s.started_at for s in segs)[:2] == [pytest.approx(T0, abs=0.5)] * 2
    # all audio of both tracks deleted after transcription, folder removed
    assert all(c["audio_path"] is None for c in pipeline.store.chunks("m1"))
    assert not (settings.audio_dir / "zoom" / "m1").exists()


def test_reassembly_breaks_on_a_missing_sequence_per_track(client, pipeline):
    upload(client, 0, wav(tone(300, 30)), track="a")
    upload(client, 2, wav(tone(300, 30)), track="a")  # 1 missing: two runs
    upload(client, 0, wav(tone(500, 30)), track="b")
    upload(client, 1, wav(tone(500, 30)), track="b")
    runs = list(pipeline._track_runs(pipeline.store.chunks("m1")))
    assert [(t, start) for t, start, _, _ in runs] == [("a", T0), ("a", T0 + 60),
                                                       ("b", T0)]


# ------------------------------------------------------------ the stop answer


def test_one_quiet_participant_never_stops_a_meeting(client):
    for seq in range(4):
        a = upload(client, seq, wav(tone(300, 30)), track="talker")
        b = upload(client, seq, wav(silence(30)), track="quiet")
        assert a.json()["stop"] is False and b.json()["stop"] is False
        assert b.json()["trailing_silence_seconds"] < 1


def test_tracked_session_never_stops_on_silence(client, settings):
    # silence stop is 60 s in the test settings; 10 minutes of silence on every track
    settings.max_session_seconds = 3600
    upload(client, 0, wav(tone(300, 30)), track="a")
    upload(client, 0, wav(tone(500, 30)), track="b")
    for seq in range(1, 21):
        for track in ("a", "b"):
            r = upload(client, seq, wav(silence(30)), track=track).json()
            assert r["stop"] is False, (seq, track, r)
    assert r["trailing_silence_seconds"] == pytest.approx(600)
    assert client.get("/v1/sessions/m1").json()["status"] == "recording"
    # an untracked chunk in a tracked session doesn't bring the silence rule back
    r = upload(client, 21, wav(silence(30)), track="").json()
    assert r["stop"] is False


def test_tracked_session_stops_at_max_duration(client, settings):
    settings.max_session_seconds = 120
    for seq in range(3):
        assert upload(client, seq, wav(silence(30)), track="a").json()["stop"] is False
    r = upload(client, 3, wav(silence(30)), track="a").json()
    assert r["stop"] is True and r["reason"] == "max_duration"
    # every track is told to stop once the session ended
    r = upload(client, 0, wav(silence(30)), track="b").json()
    assert r["stop"] is True and r["reason"] == "max_duration"


def test_tracked_session_stops_on_end(client):
    upload(client, 0, wav(silence(30)), track="a")
    assert client.post("/v1/sessions/m1/end").json() == {"ok": True, "stop": True}
    r = upload(client, 1, wav(silence(30)), track="a").json()
    assert r["stop"] is True and r["reason"] == "client"


def test_untracked_session_still_stops_on_silence(client):
    upload(client, 0, wav(tone(200, 5), silence(25)), session="s1", device="trex3pro")
    assert upload(client, 1, wav(silence(30)), session="s1",
                  device="trex3pro").json()["stop"] is False
    r = upload(client, 2, wav(silence(30)), session="s1", device="trex3pro").json()
    assert r["stop"] is True and r["reason"] == "silence"


def test_idle_finalize_ends_a_tracked_session(pipeline, settings):
    # the bot died: no uploads for CONVO_SESSION_IDLE_SECONDS (120 s in the test settings)
    now = T0 + 1000
    pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=0, started_at=T0,
                          codec="wav", data=wav(silence(30)), now=now, track="a")
    assert pipeline.finalize_idle(now + 60) == []
    assert pipeline.finalize_idle(now + 121) == ["m1"]
    sess = pipeline.store.get_session("m1")
    assert sess["status"] == "ended" and sess["stop_reason"] == "idle"


def test_late_track_joins_the_running_session(client):
    upload(client, 0, wav(tone(300, 30)), track="first")
    r = upload(client, 0, wav(tone(500, 30)), ts=T0 + 30, track="late")
    assert r.json()["stop"] is False and r.json()["duplicate"] is False
    assert r.json()["session_seconds"] == pytest.approx(60)


# ----------------------------------------------------------------- audio paths


def test_clearing_audio_touches_only_the_given_track(pipeline):
    store = pipeline.store
    for track in ("", "a", "b"):
        store.add_chunk(session_id="s", device="zoom", sequence=0, started_at=T0,
                        duration=30, codec="wav", audio_path=f"/x/{track or 'none'}",
                        speech=[], track=track)
    store.clear_chunk_audio("s", [("a", 0)])
    assert {c["track"]: c["audio_path"] for c in store.chunks("s")} == {
        "": "/x/none", "a": None, "b": "/x/b"}
    store.clear_chunk_audio("s", [("", 0)])
    assert {c["track"]: c["audio_path"] for c in store.chunks("s")} == {
        "": None, "a": None, "b": "/x/b"}


def test_retention_clears_every_track_by_its_own_key(pipeline):
    store = pipeline.store
    for track in ("a", "b"):
        store.add_chunk(session_id="s", device="zoom", sequence=0, started_at=T0,
                        duration=30, codec="wav", audio_path=f"/x/{track}", speech=[],
                        track=track)
    store.set_session_status("s", "done")
    out = store.prune_transcripts(T0 + 3600)
    assert sorted(out["audio"]) == ["/x/a", "/x/b"]
    assert all(c["audio_path"] is None for c in store.chunks("s"))


def test_rejected_upload_name_has_the_track(client, settings):
    r = upload(client, 0, b"not audio at all", track="16778240")
    assert r.status_code == 422
    names = [p.name for p in (settings.data_dir / "rejected").iterdir()]
    assert names == ["m1-16778240-0.wav.bin"]


# ------------------------------------------------------------------- migration

OLD_CHUNKS = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, device TEXT NOT NULL, started_at REAL NOT NULL,
    last_chunk_at REAL NOT NULL, last_speech_at REAL, received_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'recording', stop_reason TEXT, error TEXT,
    kind TEXT NOT NULL DEFAULT 'conversation');
CREATE TABLE chunks (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL, started_at REAL NOT NULL, duration REAL NOT NULL,
    codec TEXT NOT NULL, audio_path TEXT, speech TEXT NOT NULL DEFAULT '[]',
    received_at REAL NOT NULL, PRIMARY KEY (session_id, sequence));
INSERT INTO sessions (id, device, started_at, last_chunk_at, received_at, status)
    VALUES ('old', 'omi', 1000, 1060, 1061, 'ended');
INSERT INTO chunks VALUES ('old', 0, 1000, 30, 'opus', '/q/000000.opus', '[[1.0, 2.5]]', 1031);
INSERT INTO chunks VALUES ('old', 1, 1030, 30, 'opus', NULL, '[]', 1061);
"""


def test_old_database_with_chunks_upgrades(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_CHUNKS)
    old.execute("PRAGMA user_version = 14")
    old.close()
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 17
    rows = store.chunks("old")
    assert [(c["track"], c["sequence"], c["track_name"], c["audio_path"], c["speech"])
            for c in rows] == [("", 0, None, "/q/000000.opus", [(1.0, 2.5)]),
                               ("", 1, None, None, [])]
    pk = [r[1] for r in sorted(store._conn.execute("PRAGMA table_info(chunks)"),
                               key=lambda r: r[5]) if r[5]]
    assert pk == ["session_id", "track", "sequence"]
    # a repeated untracked chunk is still a duplicate; a tracked one is new
    with pytest.raises(DuplicateChunk):
        store.add_chunk(session_id="old", device="omi", sequence=1, started_at=1030,
                        duration=30, codec="opus", audio_path=None, speech=[])
    store.add_chunk(session_id="old", device="omi", sequence=1, started_at=1030, duration=30,
                    codec="opus", audio_path=None, speech=[], track="a")
    # the session cascade still works on the rebuilt table
    with store.tx() as c:
        c.execute("DELETE FROM sessions WHERE id = 'old'")
    assert store.chunks("old") == []
    store.close()
    # opening again is a no-op
    again = Store(path)
    assert again._conn.execute("PRAGMA user_version").fetchone()[0] == 17
    again.close()


def test_new_database_has_tracked_chunks(tmp_path):
    store = Store(tmp_path / "new.db")
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(chunks)")}
    assert {"track", "track_name"} <= cols
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 17


# ----------------------------------------------------------------------- relay


def test_relay_passes_track_fields_through(pipeline):
    mcp_token = "mcp-token-abcdefghijkl"
    mcp = build_mcp(pipeline.store, pipeline.settings)
    add_ingest_routes(mcp, pipeline.settings,
                      transport=httpx.ASGITransport(app=create_app(pipeline)))
    relay = TestClient(BearerAuth(mcp.streamable_http_app(), mcp_token))
    relay.headers["Authorization"] = f"Bearer {mcp_token}"
    r = upload(relay, 0, wav(silence(1)), path="/ingest/chunk", track="16778240",
               track_name="Alice Example")
    assert r.status_code == 200 and r.json()["track"] == "16778240"
    assert pipeline.store.tracks("m1")[0]["names"] == ["Alice Example"]
    bad = upload(relay, 1, wav(silence(1)), path="/ingest/chunk", track_name="Alice")
    assert bad.status_code == 400 and bad.json()["detail"] == "track_name needs a track"
    assert TOKEN not in r.text


def test_track_name_is_latest_by_time_not_sequence(client, pipeline):
    # QA BUG-15: the latest name is the one of the latest chunk in time (started_at, then
    # sequence), not the highest sequence; ``names`` keeps every raw name in first-use
    # order, so a later name that a cleaner would drop ("iPhone", an emoji) doesn't hide
    # an earlier one ("Conference Room 2").
    upload(client, 0, wav(silence(30)), ts=T0 + 60, track="t1", track_name="Alice Example")
    upload(client, 1, wav(silence(30)), ts=T0, track="t1", track_name="Conference Room 2")
    upload(client, 2, wav(silence(30)), ts=T0 + 30, track="t1", track_name=None)
    (t1,) = pipeline.store.tracks("m1")
    assert t1["name"] == "Alice Example"
    assert t1["names"] == ["Conference Room 2", "Alice Example"]
    upload(client, 0, wav(silence(30)), ts=T0, track="t2", track_name="Conference Room 2")
    upload(client, 1, wav(silence(30)), ts=T0 + 30, track="t2", track_name="iPhone")
    t2 = pipeline.store.tracks("m1")[1]
    assert t2["name"] == "iPhone"  # raw names at the store level; cleaning is downstream
    assert t2["names"] == ["Conference Room 2", "iPhone"]


def test_tracked_chunk_reopens_a_session_stopped_on_silence(client, pipeline):
    # QA BUG-16: an untracked chunk stopped the session on silence, then the meeting's
    # tracked chunks arrive. A tracked session never stops on silence, so the silence
    # stop doesn't stand: the session records again and the bot isn't told to stop.
    r = upload(client, 0, wav(silence(90)), track="").json()
    assert r["stop"] is True and r["reason"] == "silence"
    r = upload(client, 0, wav(tone(300, 30)), ts=T0 + 90, track="t1").json()
    assert r["stop"] is False, r
    sess = client.get("/v1/sessions/m1").json()
    assert sess["status"] == "recording" and sess["stop_reason"] is None
    assert upload(client, 1, wav(silence(30)), ts=T0 + 120,
                  track="t1").json()["stop"] is False
    # the same once the worker already processed the stopped session
    upload(client, 0, wav(silence(90)), session="m2", track="")
    pipeline.process_pending()
    assert client.get("/v1/sessions/m2").json()["status"] == "done"
    r = upload(client, 0, wav(tone(300, 30)), session="m2", ts=T0 + 90, track="t1").json()
    assert r["stop"] is False, r
    assert client.get("/v1/sessions/m2").json()["status"] == "recording"


def test_other_stops_stand_for_tracked_chunks(client, settings):
    # QA BUG-16: only a silence stop is undone; an untracked late chunk changes nothing.
    upload(client, 0, wav(silence(90)), session="s1", device="trex3pro")
    r = upload(client, 1, wav(silence(30)), session="s1", device="trex3pro").json()
    assert r["stop"] is True and r["reason"] == "silence"
    upload(client, 0, wav(silence(30)), track="a")
    client.post("/v1/sessions/m1/end")
    r = upload(client, 1, wav(silence(30)), track="a").json()
    assert r["stop"] is True and r["reason"] == "client"
