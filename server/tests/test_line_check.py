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


def _two_voice_diarizer() -> Diarizer:
    from test_recognition import unit

    return Diarizer(MixEmbedder(), [EnrolledVoice("Me", unit(1), True),
                                    EnrolledVoice("Alice", unit(0, 1), False)],
                    match_threshold=0.5, owner_hint_margin=0)


def test_match_margin_leaves_a_voice_between_two_people_unnamed():
    from test_recognition import unit

    d = _two_voice_diarizer()
    clear, between = unit(0.95, 0.31), unit(0.75, 0.66)  # Me 0.95 vs 0.31; 0.75 vs 0.66
    names = [x.name for x in d.name_clusters([clear, between])[0]]
    assert names == ["Me", "Alice"]  # off: the second still takes the free name
    d.match_margin = 0.10
    names = [x.name for x in d.name_clusters([clear, between])[0]]
    assert names == ["Me", "Speaker 1"]


def test_too_little_speech_is_not_named():
    from test_recognition import unit

    d = _two_voice_diarizer()
    d.name_min_seconds = 5.0
    labels, unnamed = d.name_clusters([unit(1), unit(0, 1)], seconds=[30.0, 3.0])
    assert [x.name for x in labels] == ["Me", "Speaker 1"] and list(unnamed) == ["Speaker 1"]
    # no durations (the ECAPA path): the rule doesn't apply
    assert [x.name for x in d.name_clusters([unit(1), unit(0, 1)])[0]] == ["Me", "Alice"]


def test_build_diarizer_sets_the_naming_rules(tmp_path, monkeypatch):
    from conftest import ToneEmbedder

    from convo_server import cli
    from convo_server.config import Settings
    from convo_server.store import Store

    monkeypatch.setattr(cli, "EcapaEmbedder", lambda cache: ToneEmbedder())
    s = Settings(ingest_token="t" * 32, data_dir=tmp_path, speaker_match_margin=0.1,
                 speaker_name_min_seconds=5, speaker_split_min_seconds=0.6)
    d = cli.build_diarizer(s, Store(tmp_path / "x.db"))
    assert (d.match_margin, d.name_min_seconds, d.split_min_seconds) == (0.1, 5.0, 0.6)


def test_voices_of_two_speaker_models_dont_mix(tmp_path):
    """Switching CONVO_SPEAKER_EMBEDDER: old (192-value) samples stay stored but the
    voiceprint follows the newest model, and a voice of the other model never matches."""
    from convo_server.diarize import load_voices
    from convo_server.store import Store

    store = Store(tmp_path / "x.db")
    old = np.ones(192, np.float32)
    store.put_speaker("Me", old, is_owner=True)
    store.add_voice_sample("Me", np.ones(192, np.float32))
    (v,) = load_voices(store)
    assert v.embedding.size == 192 and v.samples.shape == (2, 192)
    new = np.zeros(256, np.float32)
    new[0] = 1
    assert v.score(new) == -1.0  # another model: never a match
    store.put_speaker("Me", new, is_owner=True)  # enrolled again with the new model
    store.add_voice_sample("Me", new)
    (v,) = load_voices(store)
    assert v.embedding.size == 256 and v.samples.shape == (2, 256)
    assert v.score(new) > 0.99 and v.score(old) == -1.0


def test_pyannote_embedder_needs_the_pyannote_diarizer(tmp_path):
    import pytest

    from convo_server import cli
    from convo_server.config import Settings
    from convo_server.store import Store

    s = Settings(ingest_token="t" * 32, data_dir=tmp_path, speaker_embedder="pyannote")
    with pytest.raises(SystemExit):
        cli.build_diarizer(s, Store(tmp_path / "x.db"))
    s = Settings(ingest_token="t" * 32, data_dir=tmp_path, speaker_embedder="wespeaker")
    with pytest.raises(SystemExit):
        cli.build_diarizer(s, Store(tmp_path / "x.db"))


def test_pyannote_gets_the_measured_defaults_and_settings_still_win(tmp_path, monkeypatch):
    from convo_server.config import Settings

    for k in ("CONVO_VAD_THRESHOLD", "CONVO_SPEAKER_SPLIT_LINES", "CONVO_SPEAKER_LINE_CHECK",
              "CONVO_SPEAKER_SPLIT_MIN_SECONDS", "CONVO_DIARIZER"):
        monkeypatch.delenv(k, raising=False)
    base = dict(ingest_token="t" * 32, data_dir=tmp_path, _env_file=None)
    ecapa = Settings(**base)
    assert (ecapa.speaker_split_lines, ecapa.speaker_line_check, ecapa.vad_threshold,
            ecapa.speaker_split_min_seconds) == (False, False, 0.5, 1.5)
    py = Settings(**base, diarizer="pyannote")
    assert (py.speaker_split_lines, py.speaker_line_check, py.vad_threshold,
            py.speaker_split_min_seconds) == (True, True, 0.35, 0.3)
    # a value set in the environment always wins
    monkeypatch.setenv("CONVO_SPEAKER_LINE_CHECK", "false")
    monkeypatch.setenv("CONVO_VAD_THRESHOLD", "0.5")
    py = Settings(**base, diarizer="pyannote")
    assert py.speaker_line_check is False and py.vad_threshold == 0.5
    assert py.speaker_split_lines is True
