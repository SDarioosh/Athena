"""The transcription steps a recording and a meeting share.

``Pipeline`` transcribes a session with these, and the meeting code
(``meetings.transcribe``, ``meetings.people.voices``) calls the same functions, so both
paths read the same audio, cut the same runs and send whisper the same windows.

- ``load_chunk``: one chunk's stored audio, decoded.
- ``runs``: one track's runs of contiguous chunks, decoded and joined.
- ``plan_windows``: a run's whisper windows, from its VAD speech.
- ``whisper_window``: one window through whisper: its lines, their words and the
  context for the next window.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np

from . import audio as audio_mod
from . import clips
from .clips import Window, tail_words
from .config import Settings
from .diarize import PieceRef
from .transcribe import Transcriber
from .vad import Span, merge_spans

CONTIGUOUS_TOLERANCE = 1.5  # seconds between chunk end and next chunk start
REGION_MERGE_GAP = 2.0
# ASR gain (CONVO_ASR_AGC_MAX_GAIN) only touches clips whose peak is at or below this
# fraction of full scale: Omi measured more errors when already-loud audio was gained.
ASR_AGC_DEADBAND = 0.4


def load_chunk(chunk: dict[str, Any]) -> np.ndarray:
    data = Path(chunk["audio_path"]).read_bytes()
    return audio_mod.decode(data, chunk["codec"])


def runs(chunks: list[dict[str, Any]],
         load: Callable[[dict[str, Any]], np.ndarray] = load_chunk,
         ) -> Iterator[tuple[float, np.ndarray, list[Span]]]:
    """Yield (epoch_start, audio, speech_spans) for runs of contiguous chunks of
    one track, in sequence order. ``load`` decodes one chunk (``load_chunk``)."""
    run: list[dict[str, Any]] = []

    def flush():
        if not run:
            return None
        start = run[0]["started_at"]
        parts, spans, offset = [], [], 0.0
        for c in run:
            pcm = load(c)
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


def plan_windows(settings: Settings, run_speech: list[Span],
                 run_seconds: float) -> list[Window]:
    """The whisper windows of a run: its VAD speech merged into regions, padded and
    packed (``clips.plan_windows``)."""
    cfg = settings
    return clips.plan_windows(merge_spans(run_speech, REGION_MERGE_GAP), run_seconds,
                              pad=cfg.whisper_pad_seconds,
                              max_seconds=cfg.whisper_window_seconds,
                              max_gap=cfg.whisper_window_gap_seconds)


def whisper_window(settings: Settings, transcriber: Transcriber, run: int,
                   run_start: float, w: Window, clip: np.ndarray, prompt: str, context: str
                   ) -> tuple[list[tuple[float, float, str, PieceRef]],
                              list[list[tuple[float, float, str]]], str]:
    """Transcribe one window (``clip`` is ``w.extract`` of the run audio): its pieces,
    their words and the context after it. ``meeting_live`` calls this one window at a
    time while a meeting runs, so both paths send whisper the same clips."""
    cfg = settings
    pieces: list[tuple[float, float, str, PieceRef]] = []
    piece_words: list[list[tuple[float, float, str]]] = []
    # The diarizer keeps the ungained run audio; only the transcriber hears
    # the gain (CONVO_ASR_AGC_MAX_GAIN).
    clip, _ = audio_mod.bounded_gain(clip, max_gain=cfg.asr_agc_max_gain,
                                     deadband=ASR_AGC_DEADBAND)
    got = transcriber.transcribe(clip, " ".join(x for x in (prompt, context) if x))
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
