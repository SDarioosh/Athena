"""Lines that run over a change of speaker are split (CONVO_SPEAKER_SPLIT_LINES)."""

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
        return (v / np.linalg.norm(v)).astype(np.float32)


def _me() -> EnrolledVoice:
    return EnrolledVoice("Me", MixEmbedder().embed(tone(200, 2)), True)


def test_a_line_over_a_speaker_change_is_split_where_the_voice_changes():
    run = np.concatenate([tone(200, 4), tone(600, 8), tone(200, 4)])
    pieces = [PieceRef(0, 0.0, 8.0), PieceRef(0, 8.0, 12.0), PieceRef(0, 12.0, 16.0)]
    d = Diarizer(MixEmbedder(), [_me()])
    labels, _ = d.diarize_runs([run], pieces)
    words = [_words(0, 8), _words(8, 12), _words(12, 16)]
    parts = d.split_lines([run], pieces, words, labels)
    first = [(a, b, lab.name) for a, b, lab in parts[0]]
    assert first == [(0, 8, "Me"), (8, 16, "Speaker 1")]  # cut at 4.0 s
    assert parts[0][0][2].is_owner and not parts[0][1][2].is_owner
    # one voice in the line: it stays whole with its own label
    assert [(a, b, lab.name) for a, b, lab in parts[1]] == [(0, 8, "Speaker 1")]
    assert [(a, b, lab.name) for a, b, lab in parts[2]] == [(0, 8, "Me")]


def test_short_lines_and_lines_without_words_are_not_split():
    run = np.concatenate([tone(200, 4), tone(600, 4)])
    pieces = [PieceRef(0, 0.0, 4.0), PieceRef(0, 4.0, 8.0), PieceRef(0, 3.0, 5.0)]
    d = Diarizer(MixEmbedder(), [_me()])
    labels, _ = d.diarize_runs([run], pieces)
    # the last line has a change at 4.0 s but each side is under 1.5 s of speech
    parts = d.split_lines([run], pieces, [_words(0, 4), [], _words(3, 5)], labels)
    assert [len(p) for p in parts] == [1, 1, 1]
    assert parts[1] == [(0, 0, labels[1])]


def test_one_speaker_in_the_run_means_nothing_to_split():
    run = tone(200, 8)
    d = Diarizer(MixEmbedder(), [_me()])
    labels, _ = d.diarize_runs([run], [PieceRef(0, 0.0, 8.0)])
    assert d.split_lines([run], [PieceRef(0, 0.0, 8.0)], [_words(0, 8)], labels) == [
        [(0, 16, labels[0])]]


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


class WordTranscriber(FakeTranscriber):
    """One line from 0 to 8 s over two voices, one line from 8 to 12 s, with word times."""

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        self.calls += 1
        w1, w2 = _words(0, 8), _words(8, 12)
        return [TranscriptPiece(0.0, 8.0, "".join(w[2] for w in w1).strip(), tuple(w1)),
                TranscriptPiece(8.0, 12.0, "".join(w[2] for w in w2).strip(), tuple(w2))]


def test_pipeline_stores_the_parts_as_lines_when_turned_on(pipeline):
    pipeline.diarizer = Diarizer(MixEmbedder(), [])
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
