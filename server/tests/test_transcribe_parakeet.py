"""ParakeetTranscriber with a fake sherpa-onnx recognizer (no model needed)."""

from __future__ import annotations

import builtins
from types import SimpleNamespace

import numpy as np
import pytest

from convo_server import cli
from convo_server.config import Settings
from convo_server.transcribe import (
    ParakeetTranscriber,
    WhisperServerTranscriber,
    split_points,
    tokens_to_pieces,
)

SR = 16000


def result(words: list[tuple[float, str]], dur: float = 0.2) -> SimpleNamespace:
    """A sherpa-onnx-like result: one token per (start, text) word."""
    tokens = [(" " if i else "") + w for i, (_, w) in enumerate(words)]
    starts = [s for s, _ in words]
    return SimpleNamespace(text="".join(tokens), tokens=tokens, timestamps=starts,
                           durations=[dur] * len(words))


class FakeStream:
    def __init__(self) -> None:
        self.audio = np.zeros(0, dtype=np.float32)
        self.result = None

    def accept_waveform(self, sr: int, audio: np.ndarray) -> None:
        assert sr == SR
        self.audio = audio


class FakeRecognizer:
    """Says "Hello." at 0.5 s into every clip it gets; records the clip lengths."""

    def __init__(self, words: list[tuple[float, str]] | None = None) -> None:
        self.words = [(0.5, "Hello.")] if words is None else words
        self.lengths: list[float] = []

    def create_stream(self) -> FakeStream:
        return FakeStream()

    def decode_stream(self, stream: FakeStream) -> None:
        self.lengths.append(stream.audio.size / SR)
        stream.result = result(self.words)


def loud_with_pause(seconds: float, pause_at: float) -> np.ndarray:
    rng = np.random.default_rng(0)
    a = (0.3 * rng.standard_normal(int(seconds * SR))).astype(np.float32)
    a[int(pause_at * SR): int((pause_at + 0.5) * SR)] = 0.0
    return a


def test_short_clip_is_one_call_with_clip_times() -> None:
    rec = FakeRecognizer([(0.3, "Ask"), (0.6, "not."), (1.0, "Ask"), (1.4, "what?")])
    out = ParakeetTranscriber("unused", recognizer=rec).transcribe(np.zeros(5 * SR, np.float32))
    assert rec.lengths == [5.0]
    assert [p.text for p in out] == ["Ask not.", "Ask what?"]
    assert [(p.start, p.end) for p in out] == [pytest.approx((0.3, 0.8)), pytest.approx((1.0, 1.6))]


def test_long_clip_splits_in_the_pause_and_offsets_times() -> None:
    audio = loud_with_pause(70.0, pause_at=22.0)  # quietest point within 15-30 s
    rec = FakeRecognizer()
    out = ParakeetTranscriber("unused", recognizer=rec).transcribe(audio)
    assert len(rec.lengths) == 3 and all(n <= 30.0 for n in rec.lengths)
    assert sum(rec.lengths) == pytest.approx(70.0)
    first = rec.lengths[0]
    assert 22.0 <= first <= 22.5  # cut inside the silent half second
    starts = [p.start for p in out]
    assert starts == pytest.approx([0.5, first + 0.5, first + rec.lengths[1] + 0.5])
    assert all(p.text == "Hello." for p in out)


def test_split_points_cover_audio_without_gaps() -> None:
    audio = loud_with_pause(95.0, pause_at=50.0)
    ranges = split_points(audio, 30.0)
    assert ranges[0][0] == 0 and ranges[-1][1] == audio.size
    assert all(a == prev_b for (_, prev_b), (a, _) in zip(ranges, ranges[1:], strict=False))
    assert all(b - a <= 30 * SR for a, b in ranges)
    assert split_points(audio[: 10 * SR], 30.0) == [(0, 10 * SR)]


def test_sentence_and_gap_splitting() -> None:
    r = result([(0.0, "so"), (0.3, "we"), (0.6, "agree."), (1.0, "Next"), (1.3, "item"),
                (3.0, "is"), (3.3, "the"), (3.6, "budget")])
    pieces = tokens_to_pieces(r, offset=10.0, gap_seconds=0.8)
    assert [p.text for p in pieces] == ["so we agree.", "Next item", "is the budget"]
    assert pieces[0].start == pytest.approx(10.0) and pieces[0].end == pytest.approx(10.8)
    assert pieces[1].end == pytest.approx(11.5) and pieces[2].start == pytest.approx(13.0)


def test_subword_tokens_and_abbreviation_dots_stay_in_one_word() -> None:
    # "U.S." is split into subword tokens without a leading space: no break inside it.
    r = SimpleNamespace(text="", tokens=["The", " U", ".", "S", ".", " team", " won", "."],
                        timestamps=[0.0, 0.2, 0.3, 0.35, 0.4, 0.6, 0.9, 1.1],
                        durations=[0.1] * 8)
    pieces = tokens_to_pieces(r, 0.0, 0.8)
    assert [p.text for p in pieces] == ["The U.S.", "team won."]


def test_missing_durations_are_estimated() -> None:
    r = SimpleNamespace(text="Hi there.", tokens=["Hi", " there."], timestamps=[1.0, 1.4],
                        durations=[])
    (p,) = tokens_to_pieces(r, 0.0, 0.8)
    assert p.start == 1.0 and 1.4 < p.end <= 1.5


def test_empty_result_and_tiny_clip() -> None:
    rec = FakeRecognizer(words=[])
    t = ParakeetTranscriber("unused", recognizer=rec)
    assert t.transcribe(np.zeros(3 * SR, np.float32)) == []
    assert t.transcribe(np.zeros(SR // 20, np.float32)) == []  # 50 ms: not decoded
    assert rec.lengths == [3.0]


def test_prompt_is_ignored() -> None:
    t = ParakeetTranscriber("unused", recognizer=FakeRecognizer())
    clip = np.zeros(2 * SR, np.float32)
    assert t.transcribe(clip, "Odysseus, Tailscale") == t.transcribe(clip)


def test_missing_extra_names_it(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    real_import = builtins.__import__

    def no_sherpa(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("No module named 'sherpa_onnx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sherpa)
    with pytest.raises(RuntimeError, match=r"\[parakeet\]"):
        ParakeetTranscriber(tmp_path)


def test_missing_model_files(tmp_path) -> None:
    pytest.importorskip("sherpa_onnx")
    with pytest.raises(RuntimeError, match="CONVO_PARAKEET_MODEL_DIR"):
        ParakeetTranscriber(tmp_path)


def _settings(tmp_path, **kw) -> Settings:
    return Settings(ingest_token="x" * 16, data_dir=tmp_path, _env_file=None, **kw)


def test_build_transcriber_selects_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen = {}

    class Stub:
        def __init__(self, model_dir, threads) -> None:
            seen.update(model_dir=model_dir, threads=threads)

    monkeypatch.setattr(cli, "ParakeetTranscriber", Stub)
    s = _settings(tmp_path, transcriber="parakeet", parakeet_model_dir=tmp_path / "pk",
                  parakeet_threads=2, whisper_server_url="http://127.0.0.1:8178")
    assert isinstance(cli.build_transcriber(s), Stub)
    assert seen == {"model_dir": tmp_path / "pk", "threads": 2}

    whisper = _settings(tmp_path, whisper_server_url="http://127.0.0.1:8178")
    assert whisper.transcriber == "whisper" and whisper.parakeet_threads == 4
    assert isinstance(cli.build_transcriber(whisper), WhisperServerTranscriber)
    assert cli.build_transcriber(_settings(tmp_path)) is None

    with pytest.raises(RuntimeError, match="CONVO_PARAKEET_MODEL_DIR"):
        cli.build_transcriber(_settings(tmp_path, transcriber="parakeet"))
    with pytest.raises(RuntimeError, match="unknown CONVO_TRANSCRIBER"):
        cli.build_transcriber(_settings(tmp_path, transcriber="vosk"))


def test_env_selects_parakeet(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CONVO_TRANSCRIBER", "parakeet")
    monkeypatch.setenv("CONVO_PARAKEET_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("CONVO_PARAKEET_THREADS", "2")
    s = _settings(tmp_path)
    assert (s.transcriber, s.parakeet_model_dir, s.parakeet_threads) == ("parakeet", tmp_path, 2)
