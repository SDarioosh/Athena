"""Line voice check (CONVO_SPEAKER_LINE_CHECK): a named line whose own voice fits another
enrolled person at least as well loses the name."""

from __future__ import annotations

import numpy as np
from conftest import silence, tone
from test_pipeline import ingest
from test_split_lines import MixEmbedder

from convo_server.diarize import UNSURE_LABEL, Diarizer, EnrolledVoice, PieceRef, SpeakerLabel


def _voices() -> list[EnrolledVoice]:
    e = MixEmbedder()
    return [EnrolledVoice("Me", e.embed(tone(200, 2)), True),
            EnrolledVoice("Alice", e.embed(tone(600, 2)), False)]


def test_a_line_in_someone_elses_voice_loses_the_name():
    run = np.concatenate([tone(200, 4), tone(600, 4), silence(2), tone(1000, 4)])
    pieces = [PieceRef(0, 0.0, 4.0), PieceRef(0, 4.0, 8.0), PieceRef(0, 10.0, 14.0)]
    me, alice = SpeakerLabel("Me", True), SpeakerLabel("Alice", False)
    stranger = SpeakerLabel("Speaker 1", False)
    d = Diarizer(MixEmbedder(), _voices())
    # the diarizer put Alice's line under the owner (a missed turn)
    out = d.check_lines([run], pieces, [me, me, stranger])
    assert out[0] == me
    assert out[1] == SpeakerLabel(UNSURE_LABEL, False)  # not moved to Alice
    assert out[2] == stranger  # unnamed lines are left alone
    # a line in its own person's voice keeps it
    assert d.check_lines([run], pieces[1:2], [alice]) == [alice]


def test_one_enrolled_voice_means_nothing_to_compare():
    run = tone(600, 4)
    d = Diarizer(MixEmbedder(), _voices()[:1])
    me = SpeakerLabel("Me", True)
    assert d.check_lines([run], [PieceRef(0, 0.0, 4.0)], [me]) == [me]


def test_pyannote_lines_are_checked_too_when_turned_on(pipeline):
    from convo_server.diarize_pyannote import PyannoteDiarizer

    # pyannote missed the turn change: one speaker for all 18 s, mostly the owner
    turns = [(0.0, 18.0, "A")]
    pipeline.diarizer = PyannoteDiarizer(MixEmbedder(), [], segmenter=lambda audio: turns)
    e = MixEmbedder()
    pipeline.store.put_speaker("Me", e.embed(tone(200, 2)), is_owner=True)
    pipeline.store.put_speaker("Alice", e.embed(tone(600, 2)), is_owner=False)
    pipeline.settings.speaker_line_check = True
    ingest(pipeline, 0, np.concatenate([tone(200, 8), silence(6), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    speakers = [s.speaker for s in pipeline.store.session_segments("s1")]
    assert speakers[0] == "Me" and speakers[-1] == UNSURE_LABEL


def test_the_check_is_off_by_default(pipeline):
    from convo_server.diarize_pyannote import PyannoteDiarizer

    turns = [(0.0, 18.0, "A")]
    pipeline.diarizer = PyannoteDiarizer(MixEmbedder(), [], segmenter=lambda audio: turns)
    e = MixEmbedder()
    pipeline.store.put_speaker("Me", e.embed(tone(200, 2)), is_owner=True)
    pipeline.store.put_speaker("Alice", e.embed(tone(600, 2)), is_owner=False)
    ingest(pipeline, 0, np.concatenate([tone(200, 8), silence(6), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    assert UNSURE_LABEL not in {s.speaker for s in pipeline.store.session_segments("s1")}


def test_the_floor_unnames_a_line_that_fits_nobody_even_with_one_voice():
    run = np.concatenate([tone(200, 4), tone(1000, 4)])  # the owner, then someone new
    pieces = [PieceRef(0, 0.0, 4.0), PieceRef(0, 4.0, 8.0)]
    me = SpeakerLabel("Me", True)
    d = Diarizer(ThreeEmbedder(), [EnrolledVoice("Me", ThreeEmbedder().embed(tone(200, 2)),
                                                 True)])
    assert d.check_lines([run], pieces, [me, me]) == [me, me]  # off by default
    assert d.check_lines([run], pieces, [me, me], floor=0.5) == [
        me, SpeakerLabel(UNSURE_LABEL, False)]


class ThreeEmbedder(MixEmbedder):
    """Energy at 200, 600 and 1000 Hz: three distinct voices."""

    def embed(self, audio: np.ndarray) -> np.ndarray:
        spec = np.abs(np.fft.rfft(audio)) ** 2
        f = np.fft.rfftfreq(audio.size, 1 / 16000)
        v = np.array([spec[np.abs(f - c) < 50].sum() for c in (200, 600, 1000)])
        n = np.linalg.norm(v)
        return (v / n if n > 0 else np.full(3, 3 ** -0.5)).astype(np.float32)
