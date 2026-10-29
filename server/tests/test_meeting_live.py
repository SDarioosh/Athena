"""Live meeting transcription (Zoom bot lane 5D): tracks are transcribed on the worker's
ticks while the meeting records, and the result is the end-of-meeting (2A) result.
Fictional data only: tones stand in for voices, a scripted whisper for words."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import zlib
from pathlib import Path

import numpy as np
import pytest
from conftest import SAMPLE_RATE, tone

from athena import meeting_live
from athena.pipeline import Pipeline, Worker
from athena.store import Store
from athena.transcribe import TranscriptPiece

T0 = 1_760_000_000.0
SERVER = Path(__file__).resolve().parents[1]

SCRIPT = {
    300: "Hello everyone, thanks for joining.",   # Alice
    1300: "so the plan is",                        # Alice, across her chunk 0 -> 1 boundary
    400: "to ship on Friday.",                    # Alice, finishes that sentence
    500: "I can take the budget review.",         # Bo, talking over Alice
    600: "Let's ship the release on Friday.",     # Bo; Alice's mic hears it too
    800: "Any questions?",                        # Bo, last round
    700: "Sounds good to me.",                    # Cleo, the owner
    1000: "Back again, sorry.",                    # Cleo after rejoining
    900: "The room is booked until noon.",        # first voice in the room
    1100: "We can stay a bit longer.",            # second voice in the room
}


def dominant(audio: np.ndarray) -> int:
    spec = np.abs(np.fft.rfft(audio))
    return int(round(np.fft.rfftfreq(audio.size, 1 / SAMPLE_RATE)[int(np.argmax(spec))], -2))


class HashWhisper:
    """One line per clip, the words of its dominant voice. Records a checksum of every
    clip with the prompt it got, so two runs can be shown to send whisper the same
    clips with the same prompt and context."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[int, int, str]] = []  # (voice, clip checksum, prompt)
        self.fail_at = fail_at

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        if self.fail_at is not None and len(self.calls) >= self.fail_at:
            raise RuntimeError("whisper-server down")
        f = dominant(audio)
        self.calls.append((f, zlib.crc32(audio.astype(np.float32).tobytes()), prompt))
        return [TranscriptPiece(0.0, audio.size / SAMPLE_RATE, SCRIPT.get(f, f"tone {f}"))]

    def voices(self) -> list[int]:
        return [f for f, _, _ in self.calls]


def chunk(*parts: tuple[float, np.ndarray], total: float = 30.0) -> bytes:
    from conftest import wav

    out = np.zeros(int(total * SAMPLE_RATE), np.float32)
    for start, p in parts:
        i = int(start * SAMPLE_RATE)
        out[i:i + p.size] += p[:out.size - i]
    return wav(out)


def tail(f: int, start: float, seconds: float) -> tuple[float, np.ndarray]:
    """The first part of a tone that runs on into the next chunk."""
    return start, tone(f, seconds)


ALICE, BO, CLEO, ROOM = "16778240", "16779264", "16780288", "16781312"
NAMES = {ALICE: "Alice Example", BO: "Bo Example", CLEO: "cleo  example",
         ROOM: "Conference Room 2"}
_ALICE_1300 = tone(1300, 6)
# Rounds of 30 s: each track's chunk for that round as (sequence, start offset, wav).
ROUNDS = [
    {ALICE: (0, 0, chunk((0, tone(300, 6)), (27, _ALICE_1300[:3 * SAMPLE_RATE]))),
     BO: (0, 0, chunk((4, tone(500, 6)))),
     CLEO: (0, 0, chunk((22, tone(700, 4)))),
     ROOM: (0, 0, chunk((1, tone(900, 4)), (9, tone(1100, 4))))},
    {ALICE: (1, 30, chunk((0, _ALICE_1300[3 * SAMPLE_RATE:]), (5.5, tone(400, 2.5)),
                          (15, tone(600, 5, amp=0.05)))),  # Bo's voice, quietly
     BO: (1, 30, chunk((15, tone(600, 5)))),
     ROOM: (1, 30, chunk((10, tone(900, 4))))},
    # Cleo left during round 2 and rejoins: her next chunk starts a new run
    {ALICE: (2, 60, chunk()),
     BO: (2, 60, chunk((2, tone(800, 3)))),
     CLEO: (1, 60, chunk((2, tone(1000, 3))))},
]
EXPECTED = [
    ("Alice Example", False, SCRIPT[300]),
    ("Speaker 1", False, SCRIPT[900]),
    ("Bo Example", False, SCRIPT[500]),
    ("Speaker 2", False, SCRIPT[1100]),
    ("Me", True, SCRIPT[700]),
    ("Alice Example", False, "so the plan is to ship on Friday."),  # joined over windows
    ("Speaker 1", False, SCRIPT[900]),
    ("Bo Example", False, SCRIPT[600]),                             # Alice's copy dropped
    ("Bo Example", False, SCRIPT[800]),
    ("Me", True, SCRIPT[1000]),
]


def upload(pipeline, rnd, session="m1", only=None):
    for track, (seq, off, data) in ROUNDS[rnd].items():
        if only is None or track in only:
            pipeline.ingest_chunk(device="zoom", session_id=session, sequence=seq,
                                  started_at=T0 + off, codec="wav", data=data, track=track,
                                  track_name=NAMES[track])


def setup(pipeline, *, live=True, window=0.0):
    cfg = pipeline.settings
    cfg.zoom_owner_names = "Cleo Example"
    cfg.meeting_live_transcribe = live
    cfg.whisper_window_seconds = window
    cfg.whisper_context_words = 8
    cfg.join_gap_seconds = 3.0
    pipeline.transcriber = HashWhisper()
    return Worker(pipeline)


def rows(pipeline, session="m1"):
    return [(s.speaker, s.is_owner, s.text) for s in pipeline.store.session_segments(session)]


def times(pipeline, session="m1"):
    return [(s.speaker, s.text, s.started_at, s.ended_at)
            for s in pipeline.store.session_segments(session)]


def run_meeting(pipeline, *, live=True, window=0.0):
    worker = setup(pipeline, live=live, window=window)
    for rnd in range(len(ROUNDS)):
        upload(pipeline, rnd)
        worker.tick()
    during = len(pipeline.transcriber.calls)
    pipeline.end_session("m1")
    worker.tick()
    return during


def fresh(settings, tmp_path, name) -> Pipeline:
    from conftest import ToneEmbedder

    from athena.diarize import Diarizer, load_voices

    s = settings.model_copy(update={"data_dir": tmp_path / name})
    store = Store(s.db_path)
    store.put_speaker("Me", ToneEmbedder().embed(tone(200, 2)), is_owner=True)
    return Pipeline(s, store, pipeline_vad(), None, Diarizer(ToneEmbedder(),
                                                             load_voices(store)))


def pipeline_vad():
    from athena.vad import EnergyVAD

    return EnergyVAD()


# ------------------------------------------------------------- same result


@pytest.mark.parametrize("window", [0.0, 28.0])
def test_live_result_is_the_end_of_meeting_result(settings, tmp_path, window):
    """Same lines, speakers, times and order; the same clips and prompts go to whisper,
    each once; and nearly all of it happened while the meeting ran."""
    live, batch = fresh(settings, tmp_path, "live"), fresh(settings, tmp_path, "batch")
    during = run_meeting(live, live=True, window=window)
    assert run_meeting(batch, live=False, window=window) == 0  # nothing until the end
    assert times(live) == times(batch)
    if window == 0.0:
        assert rows(live) == EXPECTED
    assert sorted(live.transcriber.calls) == sorted(batch.transcriber.calls)
    # live: every window of the people's tracks was done during the meeting except the
    # last window of each open run; rooms wait for the end
    after = live.transcriber.voices()[during:]
    room = [f for f in after if f in (900, 1100)]
    assert len(after) - len(room) <= 3  # at most one window per person's track
    assert all(f not in (900, 1100) for f in live.transcriber.voices()[:during])
    # the live transcript is gone once it is in the lines
    assert live.store.live_states("m1") == {}


def test_word_across_a_chunk_boundary_is_never_cut(pipeline):
    """Alice's line runs from her chunk 0 into chunk 1: it waits for chunk 1 and goes to
    whisper whole, once."""
    worker = setup(pipeline)
    upload(pipeline, 0)
    worker.tick()
    assert 1300 not in pipeline.transcriber.voices()
    upload(pipeline, 1)
    worker.tick()
    clips = [c for c in pipeline.transcriber.calls if c[0] == 1300]
    assert len(clips) == 1
    st = meeting_live.TrackState.load(pipeline.store.live_states("m1")[ALICE])
    # the whole 6 s piece, unjoined (lines join at the end, after the bleed check)
    seg = next(r for r in st.lines if r[2].startswith(SCRIPT[1300]))
    assert seg[0] - T0 == pytest.approx(27.0, abs=0.3)
    assert seg[1] - T0 == pytest.approx(33.0, abs=0.3)


def test_context_carries_on_across_ticks(pipeline):
    worker = setup(pipeline)
    upload(pipeline, 0)
    worker.tick()
    upload(pipeline, 1)
    worker.tick()
    prompt = next(p for f, _, p in pipeline.transcriber.calls if f == 1300)
    assert "thanks for joining." in prompt  # Alice's previous window
    assert "budget" not in prompt           # never Bo's words
    assert "Alice Example, Bo Example, Cleo Example." in prompt


def test_a_late_joiner_changes_only_the_prompt_of_earlier_windows(settings, tmp_path):
    """Cleo joins in round 2: windows transcribed before that didn't have her name in
    whisper's prompt (it only helps spelling); the lines are the same."""
    def play(p, live):
        worker = setup(p, live=live)
        upload(p, 0, only={ALICE, BO, ROOM})
        worker.tick()
        upload(p, 0, only={CLEO})
        upload(p, 1)
        worker.tick()
        upload(p, 2)
        worker.tick()
        p.end_session("m1")
        worker.tick()

    live, batch = fresh(settings, tmp_path, "live"), fresh(settings, tmp_path, "batch")
    play(live, True)
    play(batch, False)
    assert times(live) == times(batch)
    assert rows(live) == EXPECTED
    first = live.transcriber.calls[0]
    assert first[0] == 300 and "Cleo Example" not in first[2]
    assert all("Cleo Example" in p for _, _, p in batch.transcriber.calls)


# ----------------------------------------------------------------- idle cost


def test_a_tick_without_a_meeting_is_one_indexed_query(pipeline):
    store = pipeline.store
    # a watch recording in progress and a finished meeting: neither is live work
    pipeline.ingest_chunk(device="omi", session_id="w", sequence=0, started_at=T0,
                          codec="wav", data=chunk((1, tone(300, 3))))
    setup(pipeline)
    upload(pipeline, 0, session="done")
    pipeline.end_session("done")
    pipeline.process_session("done")
    calls = len(pipeline.transcriber.calls)
    sql: list[str] = []
    store._conn.set_trace_callback(sql.append)
    try:
        assert pipeline.transcribe_live() == 0
    finally:
        store._conn.set_trace_callback(None)
    assert len(sql) == 1 and "FROM sessions" in sql[0]
    plan = " ".join(str(r[-1]) for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql[0]))
    assert "sessions_status" in plan, plan
    assert len(pipeline.transcriber.calls) == calls
    # switched off: not even that
    pipeline.settings.meeting_live_transcribe = False
    store._conn.set_trace_callback(sql.append)
    try:
        assert pipeline.transcribe_live() == 0
    finally:
        store._conn.set_trace_callback(None)
    assert len(sql) == 1


def test_worker_tick_imports_no_meeting_code_and_starts_nothing(tmp_path):
    """In a fresh interpreter: worker ticks with no meeting (a watch recording going on)
    never import meeting code or start a thread; a meeting imports it on its first tick,
    and once it is processed nothing of it is left running."""
    script = textwrap.dedent(f"""
        import sys, threading
        sys.path.insert(0, {str(SERVER / "tests")!r})
        from conftest import FakeTranscriber, silence, tone, wav
        from athena.config import Settings
        from athena.pipeline import Pipeline, Worker
        from athena.store import Store
        from athena.vad import EnergyVAD
        s = Settings(ingest_token="x" * 20, data_dir={str(tmp_path)!r}, run_worker=False,
                     vad_backend="energy", whisper_window_seconds=0)
        t = FakeTranscriber()
        p = Pipeline(s, Store(s.db_path), EnergyVAD(), t, None)
        w = Worker(p)
        threads = threading.active_count()
        p.ingest_chunk(device="omi", session_id="s", sequence=0, started_at=1e9,
                       codec="wav", data=wav(tone(300, 5), silence(25)))
        for _ in range(3):
            w.tick()
        meeting = [m for m in sys.modules if m.startswith("athena.meeting")]
        assert not meeting, meeting
        assert t.calls == 0 and threading.active_count() == threads
        for seq in range(2):
            p.ingest_chunk(device="zoom", session_id="m", sequence=seq,
                           started_at=2e9 + 30 * seq, codec="wav",
                           data=wav(tone(300, 5), silence(10), tone(500, 5), silence(10)),
                           track="1", track_name="Person A")
        w.tick()
        assert "athena.meetings.transcribe.live" in sys.modules
        assert t.calls == 3, t.calls  # all but the last window, during the meeting
        p.end_session("m"); w.tick()
        assert t.calls == 4, t.calls
        assert p.store.live_states("m") == {{}}
        assert threading.active_count() == threads
        print(len(p.store.session_segments("m")))
    """)
    r = subprocess.run([sys.executable, "-c", script], cwd=SERVER, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "4"


# ------------------------------------------------------------ crash, restart


def test_a_restart_mid_meeting_loses_and_duplicates_nothing(settings, tmp_path):
    batch = fresh(settings, tmp_path, "batch")
    run_meeting(batch, live=False)
    live = fresh(settings, tmp_path, "live")
    worker = setup(live)
    upload(live, 0)
    worker.tick()
    upload(live, 1)
    live.transcriber.fail_at = len(live.transcriber.calls) + 2  # dies in the 3rd window
    worker.tick()
    done = live.transcriber.calls
    # the server restarts: a new store and pipeline on the same database
    again = Pipeline(live.settings, Store(live.settings.db_path), pipeline_vad(),
                     HashWhisper(), live.diarizer, None)
    worker = Worker(again)
    worker.tick()
    upload(again, 2)
    worker.tick()
    again.end_session("m1")
    worker.tick()
    assert times(again) == times(batch)
    # each clip went to whisper once over both lives of the server
    assert sorted(done + again.transcriber.calls) == sorted(batch.transcriber.calls)


def test_a_window_saved_twice_is_still_one_line(pipeline, monkeypatch):
    """A crash between whisper's answer and saving the state redoes that window: its
    line is replaced, never added twice."""
    worker = setup(pipeline)
    upload(pipeline, 0)
    upload(pipeline, 1)
    real = pipeline.store.put_live_state
    boom = {"n": 0}

    def flaky(*a):
        boom["n"] += 1
        if boom["n"] == 2:
            raise RuntimeError("killed")
        real(*a)

    monkeypatch.setattr(pipeline.store, "put_live_state", flaky)
    worker.tick()  # the 2nd window's save fails: that tick stops
    monkeypatch.setattr(pipeline.store, "put_live_state", real)
    worker.tick()
    upload(pipeline, 2)
    worker.tick()
    pipeline.end_session("m1")
    worker.tick()
    assert rows(pipeline) == EXPECTED


def test_a_late_earlier_chunk_restarts_that_track(settings, tmp_path, caplog):
    """Chunks of a track arrive out of order: what live work assumed no longer holds, so
    the track is transcribed again, and the result is still the end-of-meeting one."""
    batch = fresh(settings, tmp_path, "batch")
    run_meeting(batch, live=False)
    live = fresh(settings, tmp_path, "live")
    worker = setup(live)
    upload(live, 1, only={ALICE})  # Alice's chunk 1 first: a run starting at sequence 1
    upload(live, 2, only={ALICE})
    worker.tick()
    assert live.store.live_states("m1")[ALICE] != meeting_live.TrackState().dump()
    upload(live, 0)                # her chunk 0 arrives late and comes first
    upload(live, 1, only={BO, ROOM})
    upload(live, 2, only={BO, CLEO})
    with caplog.at_level("INFO", logger="athena.meeting_live"):
        worker.tick()
    assert "live transcript restarted" in caplog.text
    live.end_session("m1")
    worker.tick()
    assert times(live) == times(batch)


@pytest.mark.parametrize("seed", range(6))
def test_random_meetings_match_the_end_of_meeting_result(settings, tmp_path, seed):
    """Fictional meetings with voices at random times (often across chunk boundaries),
    uploads arriving in random order and rounds late, ticks in between: live and
    end-only transcription give the same lines and send whisper the same clips."""
    rng = np.random.default_rng(seed)
    window = [0.0, 28.0][seed % 2]
    tracks = {f"t{i}": f"Person {'ABC'[i]}" for i in range(3)}
    uploads = []
    for track in tracks:
        n = 6
        audio = np.zeros(int(30 * n * SAMPLE_RATE), np.float32)
        t = float(rng.uniform(0, 5))
        while t < 30 * n - 4:
            f = int(rng.integers(3, 20)) * 100
            length = float(rng.uniform(0.8, 7))
            part = tone(f, length, amp=float(rng.choice([0.3, 0.05])))
            i = int(t * SAMPLE_RATE)
            audio[i:i + part.size] += part[:audio.size - i]
            t += length + float(rng.choice([0.6, 1.5, 2.5, 4, 12]))
        for seq in range(n):
            if seed % 3 == 0 and seq == 3:
                continue  # this track left for 30 s: a new run after it
            pcm = audio[seq * 30 * SAMPLE_RATE:(seq + 1) * 30 * SAMPLE_RATE]
            # everyone is there from the first round (a late joiner's name reaches the
            # prompt only of later windows, the one known difference)
            late = int(rng.integers(0, 2)) if seq else 0
            uploads.append((seq + late, track, seq, pcm))
    from conftest import wav

    def play(p, live):
        worker = setup(p, live=live, window=window)
        p.settings.join_gap_seconds = float(rng_join)
        for rnd in range(8):
            for _, track, seq, pcm in [u for u in uploads if u[0] == rnd]:
                p.ingest_chunk(device="zoom", session_id="m1", sequence=seq,
                               started_at=T0 + 30 * seq, codec="wav", data=wav(pcm),
                               track=track, track_name=tracks[track])
            worker.tick()
        p.end_session("m1")
        worker.tick()

    rng_join = rng.choice([1.0, 3.0])
    rng_order = rng.permutation(len(uploads))
    uploads = [uploads[i] for i in rng_order]
    live, batch = fresh(settings, tmp_path, "live"), fresh(settings, tmp_path, "batch")
    play(live, True)
    play(batch, False)
    assert times(live) == times(batch) and times(live)
    assert sorted(live.transcriber.calls) == sorted(batch.transcriber.calls)


def _pause_meeting(p, *, live):
    """Alice: "Let's ship the release" at the end of her chunk 0, a pause, then "before
    Friday afternoon" in chunk 1; Bo agrees with the same words in her pause, across his
    chunk boundary. Later Bo speaks and Alice's mic hears him quietly (true bleed)."""
    worker = setup(p, live=live)
    a0 = chunk((26.5, tone(1400, 2)))
    a1 = chunk((1.5, tone(1500, 2)), (15, tone(1700, 3, amp=0.05)))
    bo = tone(1600, 1.6)
    b0 = chunk((28.7, bo[:int(1.3 * SAMPLE_RATE)]))
    b1 = chunk((0, bo[int(1.3 * SAMPLE_RATE):]), (15, tone(1700, 3)))
    for seq, (a, b) in enumerate([(a0, b0), (a1, b1), (chunk(), chunk())]):
        for track, data in ((ALICE, a), (BO, b)):
            p.ingest_chunk(device="zoom", session_id="m1", sequence=seq,
                           started_at=T0 + 30 * seq, codec="wav", data=data, track=track,
                           track_name=NAMES[track])
        worker.tick()
    p.end_session("m1")
    worker.tick()


def test_live_bleed_is_decided_before_joining(settings, tmp_path, monkeypatch):
    """QA BUG-04 (live): live work keeps whisper's unjoined pieces, so Bo's agreement in
    Alice's pause isn't taken for bleed of her joined line; a true copy is still
    dropped and never joined back. Live and end-only give the same lines."""
    monkeypatch.setitem(SCRIPT, 1400, "Let's ship the release")
    monkeypatch.setitem(SCRIPT, 1500, "before Friday afternoon.")
    monkeypatch.setitem(SCRIPT, 1600, "Let's ship the release!")
    monkeypatch.setitem(SCRIPT, 1700, "The build is green on main.")
    live, batch = fresh(settings, tmp_path, "live"), fresh(settings, tmp_path, "batch")
    _pause_meeting(live, live=True)
    _pause_meeting(batch, live=False)
    assert rows(live) == [
        ("Alice Example", False, "Let's ship the release before Friday afternoon."),
        ("Bo Example", False, "Let's ship the release!"),
        ("Bo Example", False, "The build is green on main."),
    ]
    assert times(live) == times(batch)


# ------------------------------------------------- chunks that don't match (BUG-03)


def _live_then(pipeline, change):
    """The fixture meeting with live work done, ``change`` applied to the stored chunks,
    then the end of the meeting processed."""
    worker = setup(pipeline)
    for rnd in range(len(ROUNDS)):
        upload(pipeline, rnd)
        worker.tick()
    assert set(pipeline.store.live_states("m1")) == {ALICE, BO, CLEO}
    change(pipeline.store)
    pipeline.end_session("m1")
    worker.tick()


def test_a_duration_rounded_by_one_sample_still_finishes(pipeline):
    """QA BUG-03: a chunk's stored duration that rounds to one sample more than its
    audio (1.00004 s for 16000 samples) is the same audio: the meeting finishes with the
    same lines, the live work used."""
    def nudge(store):
        store._conn.execute("UPDATE chunks SET duration = duration + 0.00004 "
                            "WHERE track = ? AND sequence = 1", (ALICE,))

    _live_then(pipeline, nudge)
    assert pipeline.store.get_session("m1")["status"] == "done"
    assert rows(pipeline) == EXPECTED


def test_a_chunk_whose_audio_doesnt_match_falls_back_for_that_track(pipeline, caplog):
    """QA BUG-03: a chunk whose audio is much shorter than its stored duration (a
    truncated file) doesn't fail the meeting: that track's live work is dropped and the
    track is transcribed again from its chunks; the other tracks keep theirs."""
    def truncate(store):
        c = next(c for c in store.chunks("m1") if c["track"] == ALICE and c["sequence"] == 1)
        store._conn.execute("UPDATE chunks SET duration = duration + 0.5 "
                            "WHERE track = ? AND sequence = 1", (ALICE,))
        assert Path(c["audio_path"]).exists()

    with caplog.at_level("WARNING"):
        _live_then(pipeline, truncate)
    assert pipeline.store.get_session("m1")["status"] == "done"
    texts = [t for _, _, t in rows(pipeline)]
    assert SCRIPT[300] in texts and SCRIPT[800] in texts and SCRIPT[700] in texts
    assert "transcribing the track again" in caplog.text
    assert pipeline.store.live_states("m1") == {}


def test_a_missing_chunk_file_doesnt_fail_the_meeting(pipeline, caplog):
    """QA BUG-03: a chunk file gone after live work (deleted, disk error) doesn't fail
    the meeting: that track is transcribed again from the chunks it still has."""
    def delete(store):
        c = next(c for c in store.chunks("m1") if c["track"] == ALICE and c["sequence"] == 1)
        Path(c["audio_path"]).unlink()

    with caplog.at_level("WARNING"):
        _live_then(pipeline, delete)
    assert pipeline.store.get_session("m1")["status"] == "done"
    got = rows(pipeline)
    assert ("Alice Example", False, SCRIPT[300]) in got        # her chunk 0
    assert not any(SCRIPT[400] in t for _, _, t in got)         # chunk 1 is gone
    assert ("Bo Example", False, SCRIPT[800]) in got            # other tracks unchanged
    assert ("Me", True, SCRIPT[1000]) in got
    assert pipeline.store.live_states("m1") == {}


def test_a_duration_off_by_one_sample_doesnt_pause_live_work(pipeline):
    """QA BUG-03: while the meeting records, a one-sample rounding difference is no
    mismatch: live work goes on."""
    worker = setup(pipeline)
    pipeline.settings.meeting_live_budget_seconds = 600.0
    upload(pipeline, 0)
    pipeline.store._conn.execute("UPDATE chunks SET duration = duration + 0.00004 "
                                 "WHERE track = ?", (BO,))
    upload(pipeline, 1)
    worker.tick()
    assert 500 in pipeline.transcriber.voices()


# --------------------------------------------------------- rooms, audio, chat


def test_room_tracks_wait_for_the_end(pipeline):
    worker = setup(pipeline)
    for rnd in range(len(ROUNDS)):
        upload(pipeline, rnd)
        worker.tick()
    assert not {900, 1100} & set(pipeline.transcriber.voices())
    assert ROOM not in pipeline.store.live_states("m1")
    pipeline.end_session("m1")
    worker.tick()
    assert {900, 1100} <= set(pipeline.transcriber.voices())


def _named_chunks(pipeline, track, names):
    """One chunk per display name (None: not known yet) on ``track``, a voice in each,
    a tick after each. Returns how many clips whisper had heard after each tick."""
    worker = setup(pipeline)
    pipeline.settings.meeting_live_budget_seconds = 600.0
    heard = []
    for seq, name in enumerate(names):
        pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=seq,
                              started_at=T0 + 30 * seq, codec="wav",
                              data=chunk((2, tone(300 + 100 * seq, 4)), (20, tone(800, 3))),
                              track=track, track_name=name)
        worker.tick()
        heard.append(len(pipeline.transcriber.calls))
    return heard


@pytest.mark.parametrize("names", [
    ["Conference Room 2", "Alice Example"],  # a room that later set a person's name
    ["Zoom user", "Kath's iPhone"],           # no person, then a personal device
    ["Alice Example", "iPhone", "Alice Example"],  # a bare device in between
])
def test_live_skips_every_track_the_end_pass_diarizes(pipeline, names):
    """QA BUG-01/15 (live): a track is left to the end by the same rule the end pass
    uses to diarize it (any display name it ever had is a room or a device, owner names
    excepted), not by its latest name only: no live work is spent on it once a room or
    device name is known."""
    from athena.meeting_tracks import is_room_track

    heard = _named_chunks(pipeline, "t1", names)
    info = pipeline.store.tracks("m1")[0]
    assert is_room_track(info, pipeline.settings.zoom_owner_name_list)
    room_at = next(i for i, n in enumerate(names) if n in ("Conference Room 2", "Zoom user",
                                                           "iPhone"))
    assert heard[-1] == (heard[room_at - 1] if room_at else 0)


@pytest.mark.parametrize("names", [
    ["Kath's iPhone"],                         # a personal device is that person
    ["Cleo Example", "iPhone"],                # the owner's track, whatever it showed
    ["Cleo (phone)"],                          # an owner name that looks like a device
])
def test_live_works_on_people_and_the_owner(pipeline, names):
    pipeline.settings.zoom_owner_names = "Cleo Example, Cleo (phone)"
    _named_chunks(pipeline, "t1", [*names, names[-1]])
    assert pipeline.transcriber.calls
    assert "t1" in pipeline.store.live_states("m1")


def test_live_waits_for_a_track_name(pipeline):
    """QA BUG-24: a track's first chunk can arrive before its display name; live work
    waits for the name, so a track that turns out to be a room wasted nothing."""
    _named_chunks(pipeline, "t1", [None, ""])
    assert pipeline.transcriber.calls == []
    assert "t1" not in pipeline.store.live_states("m1")
    pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=2, started_at=T0 + 60,
                          codec="wav", data=chunk((2, tone(500, 4))), track="t1",
                          track_name="Conference Room 2")
    Worker(pipeline).tick()
    assert pipeline.transcriber.calls == []
    assert "t1" not in pipeline.store.live_states("m1")


def test_live_starts_once_the_track_has_a_name(pipeline):
    """QA BUG-24: once named after a person, the track's earlier chunks are done live."""
    _named_chunks(pipeline, "t1", [None, "Alice Example", "Alice Example"])
    assert 300 in pipeline.transcriber.voices()
    assert "t1" in pipeline.store.live_states("m1")


def test_audio_is_kept_until_the_end_pass_succeeds(pipeline):
    worker = setup(pipeline)
    for rnd in range(len(ROUNDS)):
        upload(pipeline, rnd)
        worker.tick()
    chunks = pipeline.store.chunks("m1")
    assert all(c["audio_path"] and Path(c["audio_path"]).exists() for c in chunks)
    # the end pass fails (whisper-server down): audio and live work stay for the retry
    pipeline.transcriber.fail_at = len(pipeline.transcriber.calls)
    pipeline.end_session("m1")
    worker.tick()
    assert pipeline.store.get_session("m1")["status"] == "failed"
    assert all(Path(c["audio_path"]).exists() for c in pipeline.store.chunks("m1"))
    assert set(pipeline.store.live_states("m1")) == {ALICE, BO, CLEO}
    before = len(pipeline.transcriber.calls)
    pipeline.transcriber.fail_at = None
    pipeline.store.set_session_status("m1", "ended")
    worker.tick()
    assert rows(pipeline) == EXPECTED
    left = pipeline.transcriber.voices()[before:]
    assert len([f for f in left if f not in (900, 1100)]) <= 3  # only what was left
    assert all(c["audio_path"] is None for c in pipeline.store.chunks("m1"))
    assert pipeline.store.live_states("m1") == {}


def test_keep_audio_keeps_it(pipeline):
    pipeline.settings.keep_audio = True
    run_meeting(pipeline)
    assert all(c["audio_path"] and Path(c["audio_path"]).exists()
               for c in pipeline.store.chunks("m1"))


def test_chat_during_the_meeting_survives(pipeline, client):
    worker = setup(pipeline)
    upload(pipeline, 0)
    worker.tick()
    r = client.post("/v1/sessions/m1/chat", json={
        "lines": [{"at": T0 + 40, "from": "Bo Example", "zoom_user_id": BO,
                   "text": "Sheet is in the drive."}], "topic": "Release sync"})
    assert r.status_code == 200, r.text
    for rnd in (1, 2):
        upload(pipeline, rnd)
        worker.tick()
    pipeline.end_session("m1")
    worker.tick()
    got = rows(pipeline)
    assert ("Bo Example", False, "Sheet is in the drive.") in got
    assert [x for x in got if x[2] != "Sheet is in the drive."] == EXPECTED


def test_a_late_chunk_after_the_end_is_still_refused(pipeline):
    run_meeting(pipeline)
    r = pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=3,
                              started_at=T0 + 90, codec="wav", data=chunk((1, tone(300, 2))),
                              track=ALICE, track_name=NAMES[ALICE])
    assert r.stop
    assert rows(pipeline) == EXPECTED


# ------------------------------------------------------- budget and summary


def test_each_tick_keeps_to_its_budget_round_robin(pipeline):
    setup(pipeline)
    upload(pipeline, 0)
    upload(pipeline, 1)
    now = [0.0]

    def clock() -> float:  # every whisper window takes 4 s
        return now[0]

    real = pipeline.transcriber.transcribe

    def slow(audio, prompt=""):
        now[0] += 4.0
        return real(audio, prompt)

    pipeline.transcriber.transcribe = slow
    ids = pipeline.store.live_meetings()
    assert meeting_live.step(pipeline, ids, 10.0, clock=clock) == 3  # 4 + 4 + 4 >= 10
    # one window of each track with work, in turn: no track waits for another (Cleo's
    # only window is the last of her open run: it waits for more audio)
    assert pipeline.transcriber.voices() == [300, 500, 1300]
    assert meeting_live.step(pipeline, ids, 10.0, clock=clock) == 1  # Alice's 400
    assert meeting_live.step(pipeline, ids, 10.0, clock=clock) == 0  # nothing ready
    assert pipeline.transcriber.voices() == [300, 500, 1300, 400]


def test_the_summary_starts_right_after_the_end(pipeline, llm_log):
    worker = setup(pipeline)
    for rnd in range(len(ROUNDS)):
        upload(pipeline, rnd)
        worker.tick()
    assert not llm_log
    pipeline.end_session("m1")
    worker.tick()  # the next tick: last windows, lines, conversation and its summary
    (conv,) = pipeline.store.session_conversations("m1")
    assert pipeline.store.get_conversation(conv)["status"] == "summarized"

