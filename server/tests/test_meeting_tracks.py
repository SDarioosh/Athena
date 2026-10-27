"""Per-track meeting transcription (Zoom bot lane 2A): lines named by track, crosstalk,
bleed, shared-room tracks, the owner, and no diarizer for named tracks.
Fictional data only: tones stand in for voices, a scripted whisper for words."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
from conftest import SAMPLE_RATE, silence, tone, wav

from convo_server.diarize import PieceRef, SpeakerLabel
from convo_server.meeting_tracks import (
    BLEED_MIN_SIMILARITY,
    BLEED_SURE_SIMILARITY,
    Line,
    find_bleed,
    is_shared_room,
    meeting_prompt,
    text_similarity,
    track_speaker,
)
from convo_server.transcribe import TranscriptPiece

T0 = 1_760_000_000.0
SERVER = Path(__file__).resolve().parents[1]

# What each fictional "voice" (tone frequency) says.
SCRIPT = {
    300: "Hello everyone, thanks for joining.",          # Alice
    500: "I can take the budget review.",                # Bo, talking over Alice
    600: "Let's ship the release on Friday.",            # Bo; Alice's mic hears it too
    700: "Sounds good to me.",                           # Cleo, the owner
    900: "The room is booked until noon.",               # first voice in the room
    1100: "We can stay a bit longer.",                   # second voice in the room
}


def dominant(audio: np.ndarray) -> int:
    spec = np.abs(np.fft.rfft(audio))
    return int(round(np.fft.rfftfreq(audio.size, 1 / SAMPLE_RATE)[int(np.argmax(spec))], -2))


class ScriptedWhisper:
    """One line per clip: the words of the clip's dominant voice."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []  # (voice, prompt)

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        f = dominant(audio)
        self.calls.append((f, prompt))
        return [TranscriptPiece(0.0, audio.size / SAMPLE_RATE, SCRIPT.get(f, f"tone {f}"))]


class NoDiarizer:
    """Fails the test if any speaker model is used."""

    def __setattr__(self, name, value):
        raise AssertionError(f"diarizer touched ({name})")

    def __getattr__(self, name):
        raise AssertionError(f"diarizer used ({name})")


def at(seconds: float, part: np.ndarray, total: float = 30.0) -> list[np.ndarray]:
    return [silence(seconds), part, silence(total - seconds - part.size / SAMPLE_RATE)]


def track_audio(*parts: tuple[float, np.ndarray], total: float = 30.0) -> bytes:
    out = np.zeros(int(total * SAMPLE_RATE), np.float32)
    for start, p in parts:
        i = int(start * SAMPLE_RATE)
        out[i:i + p.size] += p
    return wav(out)


ALICE = ("16778240", "Alice Example",
         track_audio((0, tone(300, 6)), (15, tone(600, 5, amp=0.05))))  # + Bo's bleed
BO = ("16779264", "Bo Example", track_audio((4, tone(500, 6)), (15, tone(600, 5))))
CLEO = ("16780288", "cleo  example", track_audio((22, tone(700, 4))))
ROOM = ("16781312", "Conference Room 2",
        track_audio((1, tone(900, 4)), (9, tone(1100, 4))))


def meeting(pipeline, tracks, session="m1", owners="Cleo Example, Cleo (phone)"):
    pipeline.transcriber = ScriptedWhisper()
    pipeline.settings.zoom_owner_names = owners
    for track, name, data in tracks:
        pipeline.ingest_chunk(device="zoom", session_id=session, sequence=0, started_at=T0,
                              codec="wav", data=data, track=track, track_name=name)
    pipeline.end_session(session)
    pipeline.process_session(session)
    return pipeline.store.session_segments(session)


def rows(segs):
    return [(s.speaker, s.is_owner, s.text) for s in segs]


# --------------------------------------------------------------- the fixture


def test_fictional_meeting_names_every_line(pipeline):
    """3 named participants with crosstalk, a shared room with 2 voices, one bleed."""
    segs = meeting(pipeline, [ALICE, BO, CLEO, ROOM])
    assert rows(segs) == [
        ("Alice Example", False, SCRIPT[300]),
        ("Speaker 1", False, SCRIPT[900]),          # room, first voice
        ("Bo Example", False, SCRIPT[500]),         # talks over Alice: both kept
        ("Speaker 2", False, SCRIPT[1100]),         # room, second voice
        ("Bo Example", False, SCRIPT[600]),         # Alice's quiet copy dropped
        ("Me", True, SCRIPT[700]),                  # owner: named as the owner is
    ]
    alice, _, bo = segs[:3]
    assert alice.started_at < bo.started_at < alice.ended_at  # crosstalk overlaps
    assert segs == sorted(segs, key=lambda s: s.started_at)
    # the room's voices are unnamed speakers with a stored voice: name_speaker works
    cands = pipeline.store.unnamed_candidates()
    assert sorted(c["label"] for c in cands) == ["Speaker 1", "Speaker 2"]
    conv = segs[0].conversation_id
    n, voice = pipeline.store.rename_speaker(conv, "Speaker 2", "Dee Example")
    assert n == 1 and voice is not None
    # all audio deleted after transcription
    assert all(c["audio_path"] is None for c in pipeline.store.chunks("m1"))


def test_named_tracks_never_use_the_diarizer(pipeline):
    pipeline.diarizer = NoDiarizer()
    segs = meeting(pipeline, [ALICE, BO, CLEO])
    assert [s.speaker for s in segs] == ["Alice Example", "Bo Example", "Bo Example", "Me"]


def test_meeting_never_imports_pyannote_and_plain_sessions_never_import_meeting_code(
        tmp_path):
    """In a fresh interpreter: the API, CLI and a recording without tracks never import
    meeting_tracks; a meeting of named tracks imports it but never diarize_pyannote."""
    script = textwrap.dedent(f"""
        import sys
        import numpy as np
        sys.path.insert(0, {str(SERVER / "tests")!r})
        import convo_server.api, convo_server.cli, convo_server.mcp_server
        from conftest import FakeTranscriber, silence, tone, wav
        from convo_server.config import Settings
        from convo_server.pipeline import Pipeline
        from convo_server.store import Store
        from convo_server.vad import EnergyVAD
        s = Settings(ingest_token="x" * 20, data_dir={str(tmp_path)!r}, run_worker=False,
                     vad_backend="energy", whisper_window_seconds=0)
        p = Pipeline(s, Store(s.db_path), EnergyVAD(), FakeTranscriber(), None)
        p.ingest_chunk(device="omi", session_id="s", sequence=0, started_at=1e9,
                       codec="wav", data=wav(tone(300, 5), silence(25)))
        p.end_session("s"); p.process_session("s")
        assert "convo_server.meeting_tracks" not in sys.modules, "plain session"
        for i, f in enumerate((300, 500)):
            p.ingest_chunk(device="zoom", session_id="m", sequence=0, started_at=2e9,
                           codec="wav", data=wav(tone(f, 5), silence(25)),
                           track=str(i), track_name="Person " + "AB"[i])
        p.end_session("m"); p.process_session("m")
        assert "convo_server.meeting_tracks" in sys.modules
        bad = [m for m in sys.modules if m.startswith(("convo_server.diarize_pyannote",
                                                         "pyannote", "speechbrain"))]
        assert not bad, bad
        print([x.speaker for x in p.store.session_segments("m")])
    """)
    r = subprocess.run([sys.executable, "-c", script], cwd=SERVER, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['Person A', 'Person B']"


def test_whisper_context_never_crosses_tracks(pipeline):
    pipeline.settings.whisper_context_words = 8
    meeting(pipeline, [ALICE, BO])
    calls = pipeline.transcriber.calls
    # Bo's first clip comes right after Alice's last one but hears none of her words
    bo_first = next(p for f, p in calls if f == 500)
    assert "Hello everyone" not in bo_first
    # within a track the context carries on (Bo's second clip hears his first)
    bo_second = next(p for f, p in calls if f == 600 and "budget" in p)
    assert "budget review" in bo_second
    # participants' names go to whisper for spelling
    assert "Alice Example, Bo Example." in bo_first


def test_lines_join_within_a_track_only(pipeline):
    # Alice's sentence is cut in two around Bo's interjection: her halves join, Bo's
    # line stays his own even though it sits between them in time.
    SCRIPT_CUT = {300: "so the plan is", 400: "to ship on Friday.", 500: "right"}
    pipeline.settings.join_gap_seconds = 6.0
    alice = ("a", "Alice Example", track_audio((0, tone(300, 3)), (6, tone(400, 3))))
    bo = ("b", "Bo Example", track_audio((3.5, tone(500, 1.5))))
    original = dict(SCRIPT)
    SCRIPT.update(SCRIPT_CUT)
    try:
        segs = meeting(pipeline, [alice, bo])
    finally:
        SCRIPT.clear()
        SCRIPT.update(original)
    assert rows(segs) == [("Alice Example", False, "so the plan is to ship on Friday."),
                          ("Bo Example", False, "right")]


def test_track_without_a_name_is_an_unnamed_speaker(pipeline):
    segs = meeting(pipeline, [("x1", None, track_audio((0, tone(300, 4))))], owners="")
    assert rows(segs) == [("Speaker 1", False, SCRIPT[300])]


def test_room_track_without_diarizer_keeps_the_room_name(pipeline):
    pipeline.diarizer = None
    segs = meeting(pipeline, [ROOM])
    assert {s.speaker for s in segs} == {"Conference Room 2"}


def test_owner_names_setting():
    from convo_server.config import Settings

    s = Settings(ingest_token="x" * 20, zoom_owner_names=" Cleo Example, ,Cleo (phone) ")
    assert s.zoom_owner_name_list == ("Cleo Example", "Cleo (phone)")
    assert Settings(ingest_token="x" * 20).zoom_owner_name_list == ()


def test_sessions_without_tracks_are_unchanged(pipeline):
    """A recording without tracks still goes through the diarizer, as before."""
    calls = []
    real = pipeline.diarizer.diarize_runs
    pipeline.diarizer.diarize_runs = lambda *a: calls.append(1) or real(*a)
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0, started_at=T0,
                          codec="wav", data=wav(tone(200, 5), silence(25)))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    assert calls == [1]
    assert [s.speaker for s in pipeline.store.session_segments("s1")] == ["Me"]


# ------------------------------------------------------------- pure functions


@pytest.mark.parametrize("name", [
    "Conference Room", "Boardroom", "Meeting Room 2", "Zoom Rooms", "ZoomRoom-3F",
    "iPhone", "Anna's iPhone", "Galaxy S23", "Zoom user", "Polycom", "Logitech Rally",
    "Huddle 4", "Pixel 8", "SM-G991B", "+1 555 0100", "Conf Rm A", "Cisco Webex Room",
    "Dial-in user", "MacBook Pro",
])
def test_rooms_and_devices(name):
    assert is_shared_room(name)


@pytest.mark.parametrize("name", [
    "Alice Example", "Bo Example (he/him)", "Roomi Example", "Hallie Example",
    "Max Pixelton", "Mac Example", "Owen Polyakov", "", None, "Dr. Cleo Example | Acme",
])
def test_people_are_not_rooms(name):
    assert not is_shared_room(name)


def test_track_speaker():
    owners = ["Cleo Example", "Cleo (phone)"]
    assert track_speaker(None, {"name": "  Bo   Example "}, owners) == ("Bo Example", False)
    assert track_speaker(None, {"name": "CLEO EXAMPLE"}, owners) == ("CLEO EXAMPLE", True)
    assert track_speaker(None, {"name": "Cleo"}, owners) == ("Cleo", False)
    assert track_speaker(None, {"name": None}, owners) == ("", False)
    assert track_speaker(None, {"name": "Bo"}, ["", " "]) == ("Bo", False)


def _line(track, start, end, text):
    return Line(start, end, text, PieceRef(0, 0.0, end - start), track,
                SpeakerLabel(track, False))


def test_bleed_drops_the_quieter_copy():
    lines = [_line("bo", 15.0, 20.0, "Let's ship the release on Friday."),
             _line("alice", 15.3, 19.6, "let's ship the release on friday")]
    assert find_bleed(lines, [-12.0, -31.0]) == {1}
    assert find_bleed(lines, [-31.0, -12.0]) == {0}  # the louder one is kept either way
    # the same words are dropped even when both are about as loud
    assert find_bleed(lines, [-12.0, -14.0]) == {1}
    # whisper heard only part of it on the quieter mic, with a slip
    part = [lines[0], _line("alice", 16.0, 19.0, "ship the release of Friday")]
    assert BLEED_MIN_SIMILARITY <= text_similarity(part[0].text, part[1].text) \
        < BLEED_SURE_SIMILARITY
    assert find_bleed(part, [-12.0, -30.0]) == {1}


def test_bleed_keeps_different_words():
    a = _line("bo", 15.0, 20.0, "I'll send the notes after lunch.")
    c = _line("alice", 15.2, 19.0, "Can we move the demo to Monday?")
    assert text_similarity(a.text, c.text) < BLEED_MIN_SIMILARITY
    assert find_bleed([a, c], [-12.0, -30.0]) == set()
    # nearly the same sentence, but both voices loud: two people, both lines kept
    b = _line("alice", 15.0, 20.0, "I'll send the slides after lunch.")
    assert text_similarity(a.text, b.text) < BLEED_SURE_SIMILARITY
    assert find_bleed([a, b], [-12.0, -14.0]) == set()
    # the same pair with one voice far quieter is a misheard copy: dropped
    assert find_bleed([a, b], [-12.0, -30.0]) == {1}


def test_bleed_needs_the_same_time_and_two_tracks():
    a = _line("bo", 15.0, 20.0, "Let's ship the release on Friday.")
    later = _line("alice", 40.0, 45.0, "Let's ship the release on Friday.")  # repeated
    assert find_bleed([a, later], [-12.0, -30.0]) == set()
    same_track = _line("bo", 15.0, 20.0, "Let's ship the release on Friday.")
    assert find_bleed([a, same_track], [-12.0, -30.0]) == set()


def test_meeting_prompt_adds_new_names_only():
    assert meeting_prompt("Alice Example.", ["alice example", "Bo Example", "Bo Example"],
                          30) == "Alice Example. Bo Example."
    assert meeting_prompt("", ["Bo"], 30) == "Bo."
    assert meeting_prompt("Hi.", [], 30) == "Hi."


# ------------------------------------------------------------- demo seed


def test_seed_demo_has_a_meeting_with_crosstalk(pipeline):
    sys.path.insert(0, str(SERVER / "scripts"))
    from seed_demo import ME, MEETINGS, insert_meeting

    (key, start, lines), = MEETINGS
    (conv,) = insert_meeting(pipeline.store, pipeline, key, start, lines)
    segs = pipeline.store.conversation_segments(conv)
    assert len(segs) == len(lines) and {s.device for s in segs} == {"zoom"}
    assert {s.speaker for s in segs} == {"Priya Example", "Lena Example", "Omar Example", ME}
    assert all(s.is_owner == (s.speaker == ME) for s in segs)
    overlaps = [(a.speaker, b.speaker) for a, b in zip(segs, segs[1:], strict=False)
                if b.started_at < a.ended_at]
    assert overlaps == [("Lena Example", "Omar Example")]
    assert pipeline.summarize_conversation(conv)


# ----------------------------------------------- one meeting, one conversation


def _upload(pipeline, session, seq, start, data, track=None, name=None, device="zoom"):
    return pipeline.ingest_chunk(device=device, session_id=session, sequence=seq,
                                 started_at=start, codec="wav", data=data, track=track,
                                 track_name=name)


def _finish(pipeline, session):
    pipeline.end_session(session)
    pipeline.process_session(session)
    return pipeline.store.session_conversations(session)


def test_a_long_break_inside_a_meeting_is_one_conversation(pipeline):
    pipeline.transcriber = ScriptedWhisper()
    speech = track_audio((0, tone(300, 5)))
    _upload(pipeline, "m1", 0, T0, speech, "a", "Alice Example")
    _upload(pipeline, "m1", 21, T0 + 630, speech, "a", "Alice Example")  # 10 min later
    _upload(pipeline, "m1", 21, T0 + 640, track_audio((0, tone(500, 5))), "b", "Bo Example")
    (conv,) = _finish(pipeline, "m1")
    segs = pipeline.store.conversation_segments(conv)
    assert [s.speaker for s in segs] == ["Alice Example", "Alice Example", "Bo Example"]
    assert segs[1].started_at - segs[0].ended_at > pipeline.settings.conversation_gap_seconds


def test_back_to_back_meetings_are_two_conversations(pipeline):
    pipeline.transcriber = ScriptedWhisper()
    speech = track_audio((20, tone(300, 5)))  # speech ends 25 s in
    _upload(pipeline, "m1", 0, T0, speech, "a", "Alice Example")
    (first,) = _finish(pipeline, "m1")
    _upload(pipeline, "m2", 0, T0 + 85, track_audio((0, tone(300, 5))), "a", "Alice Example")
    (second,) = _finish(pipeline, "m2")  # 1 min after the first meeting's last line
    assert first != second
    assert pipeline.store.get_conversation(first) is not None


def test_an_untracked_session_never_joins_a_meeting(pipeline):
    pipeline.transcriber = ScriptedWhisper()
    _upload(pipeline, "m1", 0, T0, track_audio((20, tone(300, 5))), "a", "Alice Example")
    (meeting_conv,) = _finish(pipeline, "m1")
    _upload(pipeline, "s1", 0, T0 + 60, track_audio((0, tone(300, 5))))
    (plain,) = _finish(pipeline, "s1")
    assert plain != meeting_conv
    # untracked sessions still join each other within the gap, as before
    _upload(pipeline, "s2", 0, T0 + 120, track_audio((0, tone(300, 5))))
    assert _finish(pipeline, "s2") == [plain]


# --------------------------------------------------------------- late chunks


def test_a_late_chunk_never_touches_an_ended_meeting(pipeline, settings):
    segs = meeting(pipeline, [ALICE, BO])
    before = rows(segs)
    chunks = len(pipeline.store.chunks("m1"))
    late = _upload(pipeline, "m1", 1, T0 + 30, track_audio((0, tone(300, 5))), BO[0], BO[1])
    assert late.stop and late.reason == "client" and not late.duplicate
    assert len(pipeline.store.chunks("m1")) == chunks  # not stored
    assert not (settings.audio_dir / "zoom" / "m1").exists()  # no file written
    assert pipeline.store.get_session("m1")["status"] == "done"  # not reprocessed
    assert rows(pipeline.store.session_segments("m1")) == before
    # a new track after the end is refused the same way
    assert _upload(pipeline, "m1", 0, T0, track_audio((0, tone(300, 5))), "zz").stop
    # a retry of a stored chunk is still a duplicate, as today
    retry = _upload(pipeline, "m1", 0, T0, ALICE[2], ALICE[0], ALICE[1])
    assert retry.duplicate and retry.stop
    assert rows(pipeline.store.session_segments("m1")) == before


def test_untracked_sessions_still_take_late_chunks(pipeline):
    pipeline.transcriber = ScriptedWhisper()
    _upload(pipeline, "s1", 0, T0, track_audio((0, tone(300, 5))), device="omi")
    _finish(pipeline, "s1")
    late = _upload(pipeline, "s1", 1, T0 + 30, track_audio((0, tone(500, 5))), device="omi")
    assert late.stop and not late.duplicate
    assert len(pipeline.store.chunks("s1")) == 2  # stored, transcribed on the next pass
    pipeline.process_session("s1")
    assert [s.text for s in pipeline.store.session_segments("s1")] == [SCRIPT[300],
                                                                        SCRIPT[500]]


def test_the_last_round_after_max_duration_is_still_stored(pipeline):
    # the first track's chunk reaches the max duration and ends the meeting; the other
    # tracks' chunks of the same round arrive just after and are kept
    pipeline.transcriber = ScriptedWhisper()
    end = pipeline.settings.max_session_seconds
    _upload(pipeline, "m1", 0, T0, track_audio((0, tone(300, 5))), "a", "Alice Example")
    first = _upload(pipeline, "m1", 1, T0 + end, track_audio((0, tone(300, 5))), "a")
    assert first.stop and first.reason == "max_duration"
    other = _upload(pipeline, "m1", 0, T0 + end, track_audio((0, tone(500, 5))), "b",
                    "Bo Example")
    assert other.stop and other.reason == "max_duration" and not other.duplicate
    assert len(pipeline.store.chunks("m1", track="b")) == 1


def test_a_chunk_racing_the_worker_is_refused(pipeline, settings):
    from convo_server.store import MeetingClosed

    _upload(pipeline, "m1", 0, T0, track_audio((0, tone(300, 5))), "a", "Alice Example")
    pipeline.end_session("m1")
    pipeline.store.set_session_status("m1", "processing")  # the worker just started
    with pytest.raises(MeetingClosed):
        pipeline.store.add_chunk(session_id="m1", device="zoom", sequence=1,
                                 started_at=T0 + 30, duration=30, codec="wav",
                                 audio_path=None, speech=[], track="a")
    pipeline._late_meeting_chunk = lambda *a: None  # the early check passed just before
    late = _upload(pipeline, "m1", 1, T0 + 30, track_audio((0, tone(300, 5))), "a")
    assert late.stop and late.reason == "client"
    assert len(pipeline.store.chunks("m1")) == 1
    assert pipeline.store.get_session("m1")["status"] == "processing"
    # only the first chunk's file: the refused upload left no file behind
    assert len(list((settings.audio_dir / "zoom" / "m1").iterdir())) == 1


# ------------------------------------------- QA: bleed before joining (BUG-04)


def _with_script(extra, fn):
    original = dict(SCRIPT)
    SCRIPT.update(extra)
    try:
        return fn()
    finally:
        SCRIPT.clear()
        SCRIPT.update(original)


def test_bleed_runs_on_unjoined_pieces_agreement_in_a_pause_is_kept(pipeline):
    """QA BUG-04: Alice says "Let's ship the release", pauses, then "before Friday
    afternoon"; Bo agrees "Let's ship the release" in her silent pause. Joined first,
    her line spanned his and the same words read as bleed. Bleed is decided on the
    pieces whisper heard, then lines join: everyone's words stay."""
    pipeline.settings.join_gap_seconds = 3.0
    words = {300: "Let's ship the release", 400: "before Friday afternoon.",
             500: "Let's ship the release!"}
    alice = ("a", "Alice Example", track_audio((0, tone(300, 2)), (4.5, tone(400, 2))))
    bo = ("b", "Bo Example", track_audio((2.2, tone(500, 1.6))))
    segs = _with_script(words, lambda: meeting(pipeline, [alice, bo]))
    assert rows(segs) == [
        ("Alice Example", False, "Let's ship the release before Friday afternoon."),
        ("Bo Example", False, "Let's ship the release!"),
    ]
    alice_line = segs[0]
    assert alice_line.started_at == pytest.approx(T0, abs=0.5)
    assert alice_line.ended_at == pytest.approx(T0 + 6.5, abs=0.5)


def test_true_bleed_is_still_dropped_and_never_joined_back(pipeline):
    """Bo's sentence reaches Alice's mic (quiet copy); Alice's own unfinished line
    right before it must not pull the dropped copy back in by joining."""
    pipeline.settings.join_gap_seconds = 3.0
    words = {300: "so I think", 600: "Let's ship the release on Friday."}
    alice = ("a", "Alice Example",
             track_audio((0, tone(300, 2)), (4.5, tone(600, 4, amp=0.05))))
    bo = ("b", "Bo Example", track_audio((4.5, tone(600, 4))))
    segs = _with_script(words, lambda: meeting(pipeline, [alice, bo]))
    assert rows(segs) == [("Alice Example", False, "so I think"),
                          ("Bo Example", False, "Let's ship the release on Friday.")]
