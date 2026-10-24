"""Lines that run over a pyannote turn change are split (CONVO_SPEAKER_SPLIT_LINES)."""

from __future__ import annotations

import numpy as np
from conftest import FakeTranscriber, ToneEmbedder, silence, tone
from test_pipeline import ingest

from convo_server.diarize import Diarizer, EnrolledVoice, PieceRef
from convo_server.transcribe import TranscriptPiece


def _words(start: float, end: float, step: float = 0.5) -> list[tuple[float, float, str]]:
    n = int(round((end - start) / step))
    return [(start + i * step, start + (i + 1) * step, f" w{i + int(start / step)}")
            for i in range(n)]


class MixEmbedder(ToneEmbedder):
    """'Voice' = how much of the clip's energy is at 200 Hz vs 600 Hz: a clip with both
    voices is between them, like a real embedding of mixed speech."""

    def embed(self, audio: np.ndarray) -> np.ndarray:
        spec = np.abs(np.fft.rfft(audio)) ** 2
        f = np.fft.rfftfreq(audio.size, 1 / 16000)
        v = np.array([spec[np.abs(f - 200) < 50].sum(), spec[np.abs(f - 600) < 50].sum()])
        v = v / (v.sum() or 1.0)
        n = np.linalg.norm(v)
        return (v / n if n > 0 else np.full(2, 2 ** -0.5)).astype(np.float32)


def _me() -> EnrolledVoice:
    return EnrolledVoice("Me", MixEmbedder().embed(tone(200, 2)), True)


def test_ecapa_keeps_every_line_whole():
    """Splitting by voice alone measured worse on far-field audio (eval/recognition.py)."""
    run = np.concatenate([tone(200, 4), tone(600, 8), tone(200, 4)])
    pieces = [PieceRef(0, 0.0, 8.0), PieceRef(0, 8.0, 12.0), PieceRef(0, 12.0, 16.0)]
    d = Diarizer(MixEmbedder(), [_me()])
    labels, _ = d.diarize_runs([run], pieces)
    words = [_words(0, 8), _words(8, 12), []]
    assert d.split_lines([run], pieces, words, labels) == [
        [(0, 16, labels[0])], [(0, 8, labels[1])], [(0, 0, labels[2])]]


def test_pyannote_lines_split_at_turn_changes_and_blips_join_their_neighbour():
    from convo_server.diarize_pyannote import PyannoteDiarizer

    run = np.concatenate([tone(200, 4), tone(600, 4), silence(1), tone(200, 3)])
    turns = [(0.0, 4.0, "A"), (4.0, 8.0, "B"), (9.0, 12.0, "A"), (10.0, 10.5, "B")]
    d = PyannoteDiarizer(MixEmbedder(), [_me()], segmenter=lambda audio: turns)
    pieces = [PieceRef(0, 0.0, 8.0), PieceRef(0, 9.0, 12.0)]
    labels, _ = d.diarize_runs([run], pieces)
    parts = d.split_lines([run], pieces, [_words(0, 8), _words(9, 12)], labels)
    assert [(a, b, lab.name) for a, b, lab in parts[0]] == [(0, 8, "Me"),
                                                             (8, 16, "Speaker 1")]
    # B's 0.5 s inside A's turn is too short to be its own line
    assert [(a, b, lab.name) for a, b, lab in parts[1]] == [(0, 6, "Me")]


def test_pyannote_short_first_part_joins_the_next_and_wordless_lines_stay():
    from convo_server.diarize_pyannote import PyannoteDiarizer

    run = np.concatenate([tone(200, 4), tone(600, 6)])
    turns = [(0.0, 4.0, "A"), (4.0, 10.0, "B")]
    d = PyannoteDiarizer(MixEmbedder(), [_me()], segmenter=lambda audio: turns)
    pieces = [PieceRef(0, 3.0, 10.0), PieceRef(0, 0.0, 3.0)]
    labels, _ = d.diarize_runs([run], pieces)
    parts = d.split_lines([run], pieces, [_words(3, 10), []], labels)
    # 1 s of A at the start is too short: the whole line is B's
    assert [(a, b, lab.name) for a, b, lab in parts[0]] == [(0, 14, "Speaker 1")]
    assert parts[1] == [(0, 0, labels[1])]


class WordTranscriber(FakeTranscriber):
    """One line from 0 to 8 s over two voices, one line from 8 to 12 s, with word times."""

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        self.calls += 1
        w1, w2 = _words(0, 8), _words(8, 12)
        return [TranscriptPiece(0.0, 8.0, "".join(w[2] for w in w1).strip(), tuple(w1)),
                TranscriptPiece(8.0, 12.0, "".join(w[2] for w in w2).strip(), tuple(w2))]


def test_pipeline_stores_the_parts_as_lines_when_turned_on(pipeline):
    from convo_server.diarize_pyannote import PyannoteDiarizer

    turns = [(0.0, 4.0, "A"), (4.0, 12.0, "B")]
    pipeline.diarizer = PyannoteDiarizer(MixEmbedder(), [], segmenter=lambda audio: turns)
    pipeline.store.put_speaker("Me", MixEmbedder().embed(tone(200, 2)), is_owner=True)
    pipeline.settings.speaker_split_lines = True
    pipeline.settings.whisper_pad_seconds = 0.0
    pipeline.transcriber = WordTranscriber()
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 8)]))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    segs = pipeline.store.session_segments("s1")
    assert [(s.speaker, s.text) for s in segs] == [
        ("Me", " ".join(f"w{i}" for i in range(8))),
        ("Speaker 1", " ".join(f"w{i}" for i in range(8, 16))),
        ("Speaker 1", " ".join(f"w{i}" for i in range(16, 24))),
    ]
    assert abs((segs[1].started_at - segs[0].started_at) - 4.0) < 0.01


def test_pipeline_keeps_whole_lines_by_default(pipeline):
    pipeline.settings.whisper_pad_seconds = 0.0
    pipeline.transcriber = WordTranscriber()
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 8)]))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    assert len(pipeline.store.session_segments("s1")) == 2


def test_pyannote_clustering_threshold_and_speaker_bounds():
    import pytest

    from convo_server.diarize_pyannote import speaker_bounds, tune_clustering

    class FakePipeline:
        def __init__(self) -> None:
            self.params = {"segmentation": {"min_duration_off": 0.0},
                           "clustering": {"threshold": 0.6, "Fa": 0.07, "Fb": 0.8}}

        def parameters(self, instantiated: bool = False) -> dict:
            return {k: dict(v) for k, v in self.params.items()}

        def instantiate(self, params: dict) -> None:
            self.params = params

    p = FakePipeline()
    tune_clustering(p, None)  # unset: the pipeline's own
    assert p.params["clustering"]["threshold"] == 0.6
    tune_clustering(p, 0.5)
    assert p.params["clustering"] == {"threshold": 0.5, "Fa": 0.07, "Fb": 0.8}
    tune_clustering(p, None, fa=0.1, fb=0.4)
    assert p.params["clustering"] == {"threshold": 0.5, "Fa": 0.1, "Fb": 0.4}
    p.params = {"segmentation": {}}
    with pytest.raises(ValueError):
        tune_clustering(p, 0.5)

    assert speaker_bounds(0, 0) == {}
    assert speaker_bounds(2, 0) == {"min_speakers": 2}
    assert speaker_bounds(1, 4) == {"min_speakers": 1, "max_speakers": 4}
    with pytest.raises(ValueError):
        speaker_bounds(3, 2)
