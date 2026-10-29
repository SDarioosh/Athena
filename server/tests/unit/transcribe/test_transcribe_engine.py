"""transcribe_engine: the shared transcription steps give what the Pipeline methods they
came from gave, for many generated inputs.

``_old_*`` below are the Pipeline methods as they were before they moved to
``athena/transcribe_engine.py`` (verbatim bodies, ``self`` passed in), kept here only to
compare against. Fictional audio and text only.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from athena import audio as audio_mod
from athena import transcribe_engine
from athena.clips import Window, plan_windows, tail_words
from athena.diarize import PieceRef
from athena.meetings.people import voices
from athena.meetings.transcribe import live
from athena.transcribe import TranscriptPiece
from athena.vad import Span, merge_spans
from tests.conftest import silence, tone, wav

CONTIGUOUS_TOLERANCE = 1.5
REGION_MERGE_GAP = 2.0
ASR_AGC_DEADBAND = 0.4


# ------------------------------------------------- the methods before the move
def _old_runs(self, chunks: list[dict[str, Any]]):
    run: list[dict[str, Any]] = []

    def flush():
        if not run:
            return None
        start = run[0]["started_at"]
        parts, spans, offset = [], [], 0.0
        for c in run:
            pcm = self._load_chunk(c)
            parts.append(pcm)
            spans += [Span(offset + s, offset + e) for s, e in c["speech"]]
            offset += pcm.size / audio_mod.SAMPLE_RATE
        return start, np.concatenate(parts) if parts else np.zeros(0, np.float32), spans

    for c in chunks:
        if c["audio_path"] is None:
            continue
        if run:
            prev = run[-1]
            gap = c["started_at"] - (prev["started_at"] + prev["duration"])
            if abs(gap) > CONTIGUOUS_TOLERANCE or c["sequence"] != prev["sequence"] + 1:
                out = flush()
                if out:
                    yield out
                run = []
        run.append(c)
    out = flush()
    if out:
        yield out


def _old_plan_windows(self, run_speech: list[Span], run_seconds: float) -> list[Window]:
    cfg = self.settings
    return plan_windows(merge_spans(run_speech, REGION_MERGE_GAP), run_seconds,
                        pad=cfg.whisper_pad_seconds,
                        max_seconds=cfg.whisper_window_seconds,
                        max_gap=cfg.whisper_window_gap_seconds)


def _old_whisper_window(self, run: int, run_start: float, w: Window, clip: np.ndarray,
                        prompt: str, context: str):
    cfg = self.settings
    pieces: list[tuple[float, float, str, PieceRef]] = []
    piece_words: list[list[tuple[float, float, str]]] = []
    clip, _ = audio_mod.bounded_gain(clip, max_gain=cfg.asr_agc_max_gain,
                                     deadband=ASR_AGC_DEADBAND)
    got = self.transcriber.transcribe(clip, " ".join(x for x in (prompt, context) if x))
    for p in (q for piece in got for q in w.split(piece)):
        s = w.to_run(p.start)
        e = max(s, w.to_run(max(p.end, p.start), end=True))
        s, e = w.clamp(s, e)
        pieces.append((run_start + s, run_start + e, p.text, PieceRef(run, s, e)))
        piece_words.append([(w.to_run(a), w.to_run(b, end=True), t)
                            for a, b, t in p.words])
    if cfg.whisper_context_words > 0 and got:
        context = tail_words(" ".join([context] + [p.text for p in got]),
                             cfg.whisper_context_words)
    return pieces, piece_words, context


# ------------------------------------------------------------- generated input
def _fake_load(chunk: dict[str, Any]) -> np.ndarray:
    """Distinct audio per chunk, its length from the stored duration."""
    n = round(chunk["duration"] * audio_mod.SAMPLE_RATE)
    return np.full(n, chunk["sequence"] / 100.0, np.float32)


def _chunks(rng: random.Random) -> list[dict[str, Any]]:
    out, t = [], rng.uniform(0, 1e9)
    seq = rng.randint(0, 3)
    for _ in range(rng.randint(0, 9)):
        dur = rng.choice([0.0, 0.01, 0.02, rng.uniform(0, 0.05)])
        out.append({
            "sequence": seq,
            "started_at": t,
            "duration": dur,
            "codec": "wav",
            "audio_path": rng.choice(["a.wav", "a.wav", "a.wav", None]),
            "speech": [(a, a + rng.uniform(0, 0.01))
                       for a in sorted(rng.uniform(0, dur) for _ in range(rng.randint(0, 3)))],
        })
        seq += rng.choice([1, 1, 1, 1, 2, 0, -1])
        # mostly contiguous, sometimes a gap or an overlap around the 1.5 s tolerance
        t += dur + rng.choice([0.0, 0.0, 0.3, -0.2, 1.4999, 1.5, 1.5001, -1.5001, 5.0])
    if rng.random() < 0.3:
        rng.shuffle(out)
    return out


def _same_runs(a, b) -> None:
    assert len(a) == len(b)
    for (s1, au1, sp1), (s2, au2, sp2) in zip(a, b, strict=True):
        assert s1 == s2 and sp1 == sp2
        assert au1.dtype == au2.dtype and np.array_equal(au1, au2)


def _settings(rng: random.Random) -> SimpleNamespace:
    return SimpleNamespace(whisper_pad_seconds=rng.choice([0.0, 0.2, 0.5]),
                           whisper_window_seconds=rng.choice([0.0, 5.0, 30.0]),
                           whisper_window_gap_seconds=rng.choice([0.0, 0.5, 2.0]),
                           asr_agc_max_gain=rng.choice([1.0, 4.0]),
                           whisper_context_words=rng.choice([0, 3, 30]))


class _WordsTranscriber:
    """Pieces with word times, so windows that join cut-apart regions split them."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.calls: list[tuple[float, str]] = []

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        self.calls.append((float(np.abs(audio).max(initial=0.0)), prompt))
        secs = audio.size / audio_mod.SAMPLE_RATE
        out = []
        for i in range(self.rng.randint(0, 3)):
            a = self.rng.uniform(-0.1, secs)
            b = a + self.rng.uniform(-0.05, secs / 2 + 0.1)
            words = tuple(sorted((x, x + 0.05, f" w{i}{j}") for j, x in
                                 enumerate(self.rng.uniform(a, max(a, b)) for _ in range(3))))
            out.append(TranscriptPiece(a, b, f"piece {i}", words if i % 2 else ()))
        return out


# ------------------------------------------------------------------------ tests
def test_runs_is_the_old_pipeline_runs(pipeline, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_load_chunk", _fake_load)
    old_self = SimpleNamespace(_load_chunk=_fake_load)
    rng = random.Random(20261029)
    for _ in range(3000):
        chunks = _chunks(rng)
        old = list(_old_runs(old_self, chunks))
        _same_runs(list(transcribe_engine.runs(chunks, _fake_load)), old)
        _same_runs(list(pipeline._runs(chunks)), old)  # the Pipeline delegate


def test_runs_decodes_with_load_chunk(tmp_path: Path) -> None:
    pcm = np.concatenate([tone(300, 1), silence(1)])
    path = tmp_path / "c.wav"
    path.write_bytes(wav(pcm))
    chunk = {"sequence": 0, "started_at": 5.0, "duration": 2.0, "codec": "wav",
             "audio_path": str(path), "speech": [(0.0, 1.0)]}
    got = transcribe_engine.load_chunk(chunk)
    assert np.array_equal(got, audio_mod.decode(path.read_bytes(), "wav"))
    [(start, run_audio, spans)] = list(transcribe_engine.runs([chunk]))
    assert start == 5.0 and np.array_equal(run_audio, got) and spans == [Span(0.0, 1.0)]


def test_plan_windows_is_the_old_pipeline_method(pipeline) -> None:
    rng = random.Random(7)
    for _ in range(3000):
        cfg = _settings(rng)
        spans = [Span(a, a + rng.uniform(0, 4))
                 for a in sorted(rng.uniform(0, 60) for _ in range(rng.randint(0, 8)))]
        seconds = rng.uniform(0, 70)
        old = _old_plan_windows(SimpleNamespace(settings=cfg), spans, seconds)
        assert transcribe_engine.plan_windows(cfg, spans, seconds) == old
        pipeline.settings = cfg
        assert pipeline._plan_windows(spans, seconds) == old


def test_whisper_window_is_the_old_pipeline_method(pipeline) -> None:
    rng = random.Random(11)
    for i in range(1500):
        cfg = _settings(rng)
        seconds = rng.uniform(0.5, 40)
        spans = [Span(a, a + rng.uniform(0.1, 3))
                 for a in sorted(rng.uniform(0, seconds) for _ in range(rng.randint(1, 6)))]
        windows = plan_windows(spans, seconds, pad=cfg.whisper_pad_seconds,
                               max_seconds=cfg.whisper_window_seconds,
                               max_gap=cfg.whisper_window_gap_seconds)
        if not windows:
            continue
        w = rng.choice(windows)
        audio = (np.sin(np.arange(int(seconds * 16000)) / 7.0)
                 * rng.choice([0.05, 0.3, 0.9])).astype(np.float32)
        clip = w.extract(audio)
        prompt, context = rng.choice(["", "Ada, Bo."]), rng.choice(["", "said before"])
        args = (i % 5, 1e9 + i, w, clip, prompt, context)
        seed = rng.random()
        old_t, new_t, del_t = (_WordsTranscriber(random.Random(seed)) for _ in range(3))
        old = _old_whisper_window(SimpleNamespace(settings=cfg, transcriber=old_t), *args)
        assert transcribe_engine.whisper_window(cfg, new_t, *args) == old
        pipeline.settings, pipeline.transcriber = cfg, del_t
        assert pipeline._whisper_window(*args) == old
        assert new_t.calls == old_t.calls == del_t.calls  # same gained clip, same prompt


def test_the_meeting_run_rules_cut_like_runs_on_their_common_input() -> None:
    """``live.plan_runs(final=True)`` and ``voices.runs`` are kept apart from
    ``transcribe_engine.runs`` (they differ on unsorted chunks, chunks without audio and
    a NaN gap); on sorted chunks that all have audio and finite times, all three cut the
    same runs."""
    rng = random.Random(3)
    for _ in range(3000):
        chunks = sorted((c for c in _chunks(rng) if c["audio_path"]),
                        key=lambda c: c["sequence"])
        # the engine yields audio, not chunks: compare the chunks each run read
        engine = [[c["sequence"] for c in run] for run in _group(chunks)]
        assert [[c["sequence"] for c in r.chunks]
                for r in live.plan_runs(chunks, final=True)] == engine
        assert [[c["sequence"] for c in r] for r in voices.runs(chunks)] == engine


def _group(chunks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The chunks of each run ``transcribe_engine.runs`` yields, in order."""
    seen: list[list[dict[str, Any]]] = []

    def load(c: dict[str, Any]) -> np.ndarray:
        seen[-1].append(c)
        return np.zeros(0, np.float32)

    out: list[list[dict[str, Any]]] = []
    gen = transcribe_engine.runs(chunks, load)
    while True:
        seen.append([])
        try:
            next(gen)
        except StopIteration:
            break
        out.append(seen[-1])
    return out
