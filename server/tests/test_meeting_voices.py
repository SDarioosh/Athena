"""Learning voices from meetings (Zoom bot lane 2C): which windows of which tracks become
voice samples, how they are stored and capped, the owner, merges, load_voices and the
relabel of unnamed speakers in other recordings. Fictional data only: tones stand in for
voices (ToneEmbedder: one voice per 100 Hz bucket)."""

from __future__ import annotations

import logging

import numpy as np
import pytest
from conftest import SAMPLE_RATE, ToneEmbedder, dominant_freq, silence, tone, wav

from convo_server.diarize import load_voices
from convo_server.meeting_voices import (
    MAX_MEETING_SAMPLES,
    candidate_windows,
    merge,
    subtract,
)

T0 = 1_760_000_000.0


class RecordingEmbedder(ToneEmbedder):
    def __init__(self) -> None:
        self.clips: list[np.ndarray] = []

    def embed(self, audio: np.ndarray) -> np.ndarray:
        self.clips.append(audio)
        return super().embed(audio)


@pytest.fixture
def rec(pipeline) -> RecordingEmbedder:
    e = RecordingEmbedder()
    pipeline.diarizer.embedder = e
    return e


def track(*parts: tuple[float, float, float], total: float = 30.0) -> bytes:
    """(start, seconds, freq) tones on one 30 s track."""
    out = np.zeros(int(total * SAMPLE_RATE), np.float32)
    for start, secs, f in parts:
        i = int(start * SAMPLE_RATE)
        t = tone(f, secs)
        out[i:i + t.size] += t
    return wav(out)


def meeting(pipeline, tracks, session="m1", owners=""):
    pipeline.settings.zoom_owner_names = owners
    for tid, name, data in tracks:
        pipeline.ingest_chunk(device="zoom", session_id=session, sequence=0, started_at=T0,
                              codec="wav", data=data, track=tid, track_name=name)
    pipeline.end_session(session)
    pipeline.process_session(session)


def sources(store, name):
    return [r["source"] for r in store.voice_samples(name)]


# ------------------------------------------------------------ window choice


def test_interval_helpers():
    assert merge([(5, 6), (0, 1), (1.2, 2)], gap=0.5) == [(0, 2), (5, 6)]
    assert subtract([(0, 10)], [(2, 3), (5, 6)]) == [(0, 2), (3, 5), (6, 10)]
    assert subtract([(0, 4)], [(-1, 5)]) == []


def test_windows_come_from_own_speech_never_while_others_speak():
    own = [{"track": "a", "sequence": 0, "started_at": 100.0, "duration": 30.0,
            "speech": [(0.0, 12.0)]}]
    others = [{"track": "b", "sequence": 0, "started_at": 100.0, "duration": 30.0,
               "speech": [(4.0, 5.0)]}]
    wins = [(a - 100, b - 100) for a, b, _ in candidate_windows(own, others, [(100.0, 130.0)])]
    # 4-5 s is b's speech, padded by 0.5 s: windows only in 0-3.5 and 5.5-12
    assert wins == [(0.0, 3.0), (5.5, 8.5), (8.5, 11.5)]


def test_samples_per_named_track_from_its_own_speech(pipeline, rec):
    # Alice 0-16 s, Bo talks over her 6-8 s and then 18-28 s
    meeting(pipeline, [("a", "Alice Example", track((0, 16, 300))),
                       ("b", "Bo Example", track((6, 2, 500), (18, 10, 500)))])
    store = pipeline.store
    assert sources(store, "Alice Example") == ["meeting"] * 3
    assert sources(store, "Bo Example") == ["meeting"] * 3
    assert all(c.size == 3 * SAMPLE_RATE for c in rec.clips)
    # each window is that participant's own voice only, never during the other's speech
    assert sorted(dominant_freq(c) for c in rec.clips) == [300] * 3 + [500] * 3
    for c in rec.clips:
        f = np.abs(np.fft.rfft(c))
        bins = np.fft.rfftfreq(c.size, 1 / SAMPLE_RATE)
        other = 500 if dominant_freq(c) == 300 else 300
        assert f[np.argmin(np.abs(bins - other))] < 0.01 * f.max()
    # both are now known voices with samples; nothing else changed for the owner
    voices = {v.name: v for v in load_voices(store)}
    assert voices["Alice Example"].samples.shape[0] == 3
    assert [v.name for v in voices.values() if v.is_owner] == ["Me"]


def meeting_samples(store) -> int:
    with store.tx() as c:
        return c.execute("SELECT COUNT(*) FROM voice_samples WHERE source = 'meeting'"
                         ).fetchone()[0]


def test_rooms_placeholders_and_short_tracks_are_skipped(pipeline):
    meeting(pipeline, [("r", "Conference Room 2", track((0, 14, 900))),
                       ("p", "iPhone", track((15, 14, 1100))),
                       ("n", None, track((0, 14, 1300))),
                       ("s", "Shy Example", track((0, 9, 700)))])  # 9 s < 10 s
    assert meeting_samples(pipeline.store) == 0  # (the rooms were diarized as before)
    names = {s["name"] for s in pipeline.store.speakers()}
    assert names.isdisjoint({"Conference Room 2", "iPhone", "Shy Example"})
    assert not any(n.startswith("Speaker") for n in names)


def test_owner_samples_go_to_the_enrolled_owner_never_a_second_owner(pipeline, rec):
    meeting(pipeline, [("o", "Cleo Example", track((0, 14, 200)))], owners="Cleo Example")
    store = pipeline.store
    assert sources(store, "Me") == ["enrolled"] + ["meeting"] * 3
    assert [s["name"] for s in store.speakers() if s["is_owner"]] == ["Me"]
    assert "Cleo Example" not in {s["name"] for s in store.speakers()}


def test_owner_without_enrolled_voice_is_not_learned(pipeline, rec, caplog):
    with pipeline.store.tx() as c:
        c.execute("UPDATE speakers SET is_owner = 0")
    with caplog.at_level(logging.INFO, "convo_server.meeting_voices"):
        meeting(pipeline, [("o", "Cleo Example", track((0, 14, 700)))],
                owners="Cleo Example")
    assert rec.clips == []
    assert not any(s["is_owner"] for s in pipeline.store.speakers())
    assert "no enrolled owner voice" in caplog.text


# ------------------------------------------------------------- settings


def test_setting_false_stores_nothing(pipeline, rec):
    pipeline.settings.learn_meeting_voices = False
    meeting(pipeline, [("a", "Alice Example", track((0, 14, 300)))])
    assert rec.clips == []
    assert "Alice Example" not in {s["name"] for s in pipeline.store.speakers()}


def test_no_diarizer_skips_quietly_once(pipeline, caplog):
    pipeline.diarizer = None
    with caplog.at_level(logging.INFO, "convo_server.meeting_voices"):
        meeting(pipeline, [("a", "Alice Example", track((0, 14, 300))),
                           ("b", "Bo Example", track((15, 14, 500)))])
    assert "Alice Example" not in {s["name"] for s in pipeline.store.speakers()}
    assert caplog.text.count("voices not learned (diarization is off)") == 1


def test_pyannote_embedder_is_never_used(pipeline, rec, caplog):
    pipeline.settings.speaker_embedder = "pyannote"
    with caplog.at_level(logging.INFO, "convo_server.meeting_voices"):
        meeting(pipeline, [("a", "Alice Example", track((0, 14, 300)))])
    assert rec.clips == []
    assert "not ecapa" in caplog.text


# --------------------------------------------------------------- storage


def test_oldest_meeting_samples_replaced_first_enrolled_kept(pipeline):
    store, e = pipeline.store, ToneEmbedder()
    store.put_speaker("Alice Example", e.embed(tone(300, 3)), False,
                      samples=[e.embed(tone(300, 3)), e.embed(tone(310, 3))])
    store.add_voice_sample("Alice Example", e.embed(tone(320, 3)))  # name_speaker
    v = e.embed(tone(300, 3))
    assert store.put_meeting_voice_samples("Alice Example", "m1", [v] * 3, 3) == 3
    assert store.put_meeting_voice_samples("Alice Example", "m2", [v] * 2, 3) == 3
    rows = store.voice_samples("Alice Example")
    assert [r["source"] for r in rows] == ["enrolled", "enrolled", "named"] + ["meeting"] * 3
    assert [r["session_id"] for r in rows if r["source"] == "meeting"] == ["m1", "m2", "m2"]
    # processing a meeting again replaces that meeting's own samples
    store.put_meeting_voice_samples("Alice Example", "m2", [v], 3)
    rows = store.voice_samples("Alice Example")
    assert [r["session_id"] for r in rows if r["source"] == "meeting"] == ["m1", "m2"]
    assert [r["source"] for r in rows][:3] == ["enrolled", "enrolled", "named"]
    assert MAX_MEETING_SAMPLES == 3


def test_meeting_samples_never_change_who_is_owner(pipeline):
    store, v = pipeline.store, ToneEmbedder().embed(tone(200, 3))
    store.put_meeting_voice_samples("Me", "m1", [v], 3)
    store.put_meeting_voice_samples("New Person", "m1", [v], 3)
    assert [s["name"] for s in store.speakers() if s["is_owner"]] == ["Me"]
    assert store.person_id("New Person") is not None


def test_merge_keeps_meeting_samples(pipeline):
    store, e = pipeline.store, ToneEmbedder()
    store.put_speaker("Kath", e.embed(tone(300, 3)), False)
    store.put_meeting_voice_samples("Kath Example", "m1", [e.embed(tone(300, 3))] * 2, 3)
    # Kath Example has lines so rename_everywhere has work to do
    meeting(pipeline, [("k", "Kath Example", track((0, 4, 300)))], session="m9")
    n, _ = store.rename_everywhere("Kath Example", "Kath")
    assert n
    assert sources(store, "Kath") == ["enrolled", "meeting", "meeting"]
    assert "Kath Example" not in {s["name"] for s in store.speakers()}


def test_load_voices_enrolled_meeting_or_both(pipeline):
    store, e = pipeline.store, ToneEmbedder()
    v3, v5 = e.embed(tone(300, 3)), e.embed(tone(500, 3))
    store.put_speaker("Enrolled Only", v3, False, samples=[v3, v3])
    store.put_meeting_voice_samples("Meeting Only", "m1", [v5, v5, v5], 3)
    store.put_speaker("Both", v3, False, samples=[v3])
    store.put_meeting_voice_samples("Both", "m1", [v5], 3)
    voices = {v.name: v for v in load_voices(store)}
    assert voices["Enrolled Only"].samples.shape[0] == 2
    assert voices["Meeting Only"].samples.shape[0] == 3
    assert voices["Both"].samples.shape[0] == 2
    assert voices["Enrolled Only"].score(v3) == pytest.approx(1.0, abs=1e-5)
    assert voices["Meeting Only"].score(v5) == pytest.approx(1.0, abs=1e-5)
    # the closest sample decides: either voice of "Both" matches it
    assert voices["Both"].score(v3) == pytest.approx(1.0, abs=1e-5)
    assert voices["Both"].score(v5) == pytest.approx(1.0, abs=1e-5)


# --------------------------------------------------------------- relabel


def test_meeting_relabels_unnamed_speaker_in_an_earlier_recording(pipeline, rec):
    # last week, in person: an unknown voice (300 Hz) and someone else (900 Hz)
    pipeline.ingest_chunk(device="omi", session_id="old", sequence=0, started_at=T0 - 7 * 86400,
                          codec="wav", data=wav(tone(300, 5), silence(5), tone(900, 5),
                                                silence(15)))
    pipeline.end_session("old")
    pipeline.process_session("old")
    before = {s.speaker for s in pipeline.store.session_segments("old")}
    assert before == {"Speaker 1", "Speaker 2"}
    # the meeting teaches Athena that 300 Hz is Alice
    meeting(pipeline, [("a", "Alice Example", track((0, 14, 300)))])
    after = sorted((s.speaker, dominant_freq_text(s.text))
                   for s in pipeline.store.session_segments("old"))
    assert after == [("Alice Example", 300), ("Speaker 2", 900)]


def test_no_relabel_when_setting_is_off(pipeline):
    pipeline.ingest_chunk(device="omi", session_id="old", sequence=0, started_at=T0 - 86400,
                          codec="wav", data=wav(tone(300, 5), silence(25)))
    pipeline.end_session("old")
    pipeline.process_session("old")
    pipeline.settings.learn_meeting_voices = False
    meeting(pipeline, [("a", "Alice Example", track((0, 14, 300)))])
    assert {s.speaker for s in pipeline.store.session_segments("old")} == {"Speaker 1"}


def dominant_freq_text(text: str) -> int:
    return int(text.split()[2])  # FakeTranscriber: "words at <freq> hz"


def test_room_voice_of_the_same_meeting_is_not_relabeled(pipeline, rec):
    # the room's unnamed voice sounds like Alice, but she has her own lines in this
    # recording: two labels of one recording are two people (relabel_matches' rule)
    meeting(pipeline, [("a", "Alice Example", track((0, 14, 300))),
                       ("r", "Conference Room 2", track((16, 5, 300)))])
    speakers = sorted({s.speaker for s in pipeline.store.session_segments("m1")})
    assert speakers == ["Alice Example", "Speaker 1"]
    assert sources(pipeline.store, "Alice Example") == ["meeting"] * 3


# ------------------------------------------------- QA fixes (Antigravity audit)


def test_track_that_was_ever_a_room_is_never_learned(pipeline, rec):
    # QA BUG-01: a room track renamed to a person stays a room for transcription
    # (diarized); learning must take the same decision and never save its voices
    pipeline.settings.zoom_owner_names = ""
    for seq, name in enumerate(["Conference Room 2", "Alice Example"]):
        pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=seq,
                              started_at=T0 + 30 * seq, codec="wav",
                              data=track((0, 14, 300)), track="r", track_name=name)
    pipeline.end_session("m1")
    pipeline.process_session("m1")
    assert meeting_samples(pipeline.store) == 0  # (rec also sees the diarizer's windows)


@pytest.mark.parametrize("name", ["Polycom", "Logitech Rally", "Huddle 4", "Conf Rm A",
                                  "ZoomRoom-3F", "SM-G991B", "Lobby", "Speakerphone"])
def test_room_and_hardware_names_are_never_learned(pipeline, rec, name):
    # QA BUG-01 (pinning: the merged room rule already makes these rooms)
    meeting(pipeline, [("r", name, track((0, 14, 300)))])
    assert meeting_samples(pipeline.store) == 0  # (rec also sees the diarizer's windows)


@pytest.mark.parametrize("name", ["Speaker 3", "Unknown", "Zoom user"])
def test_placeholder_tracks_are_never_learned(pipeline, rec, name):
    meeting(pipeline, [("p", name, track((0, 14, 300)))])
    assert meeting_samples(pipeline.store) == 0  # (rec also sees the diarizer's windows)


class DropsTranscriber:
    """FakeTranscriber that hears no words in one tone (laughter, a muted neighbour's
    murmur whisper returns nothing for)."""

    def __init__(self, inner, silent_freq: int) -> None:
        self.inner, self.silent_freq = inner, silent_freq

    def transcribe(self, audio, prompt=""):
        if dominant_freq(audio) == self.silent_freq:
            return []
        return self.inner.transcribe(audio, prompt)


def test_only_windows_with_kept_lines_of_the_track_are_learned(pipeline, rec):
    # QA BUG-05: Alice (300 Hz) speaks 0-12 s; then, while she is silent, a colleague
    # in her office who is muted on Zoom (900 Hz, louder) talks into her open mic for
    # 14 s. No transcript line comes of it, so it must never become Alice's voice.
    pipeline.transcriber = DropsTranscriber(pipeline.transcriber, 900)
    loud = np.zeros(30 * SAMPLE_RATE, np.float32)
    loud[:12 * SAMPLE_RATE] = tone(300, 12, amp=0.1)
    loud[16 * SAMPLE_RATE:30 * SAMPLE_RATE] = tone(900, 14, amp=0.6)
    meeting(pipeline, [("a", "Alice Example", wav(loud))])
    assert {s.speaker for s in pipeline.store.session_segments("m1")} == {"Alice Example"}
    assert rec.clips and {dominant_freq(c) for c in rec.clips} == {300}


def test_windows_need_a_kept_line_of_the_track():
    # QA BUG-05: VAD speech alone is not enough; a window must lie in the track's kept
    # lines (a line dropped as bleed or never transcribed is not in them)
    own = [{"track": "a", "sequence": 0, "started_at": 100.0, "duration": 30.0,
            "speech": [(0.0, 20.0)]}]
    wins = [(a - 100, b - 100) for a, b, _ in candidate_windows(own, [], [(106.0, 113.0)])]
    assert wins == [(6.0, 9.0), (9.0, 12.0)]
    assert candidate_windows(own, [], []) == []


def test_personal_device_track_is_named_but_not_learned(pipeline, rec):
    # QA BUG-05: "Anna's iPhone" is Anna for her lines, but a phone is passed around
    # and put on speakerphone, so its audio never becomes Anna's voiceprint
    meeting(pipeline, [("a", "Anna's iPhone", track((0, 14, 300))),
                       ("k", "Kath (mobile)", track((15, 14, 500)))])
    speakers = {s.speaker for s in pipeline.store.session_segments("m1")}
    assert speakers == {"Anna", "Kath"}
    assert meeting_samples(pipeline.store) == 0  # (rec also sees the diarizer's windows)


def test_name_like_device_words_still_learn(pipeline, rec):
    # "Mac" and "Dell" alone are names (QA BUG-09), not devices
    meeting(pipeline, [("m", "Mac Example", track((0, 14, 300)))])
    assert sources(pipeline.store, "Mac Example") == ["meeting"] * 3
