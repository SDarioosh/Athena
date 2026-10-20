"""Voice activity detection.

``SileroVAD`` is the production backend. ``EnergyVAD`` is a crude RMS-threshold
detector that exists only for tests / machines without torch; select it
explicitly with ``CONVO_VAD_BACKEND=energy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

from .audio import SAMPLE_RATE

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True)
class Span:
    start: float  # seconds, relative to the audio buffer
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class VAD(Protocol):
    def detect(self, audio: np.ndarray) -> list[Span]: ...


class SileroVAD:
    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 500,
                 speech_pad_ms: int = 30, min_speech_ms: int = 250) -> None:
        try:
            import torch
            from silero_vad import get_speech_timestamps, load_silero_vad
        except ImportError as e:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Silero VAD needs the 'vad' extra: pip install -e '.[vad]'"
            ) from e
        self._torch = torch
        self._get_ts = get_speech_timestamps
        self._model = load_silero_vad()
        self._threshold = threshold
        self._min_silence_ms = min_silence_ms
        self._speech_pad_ms = speech_pad_ms
        self._min_speech_ms = min_speech_ms

    def detect(self, audio: np.ndarray) -> list[Span]:
        if audio.size < SAMPLE_RATE // 10:
            return []
        stamps = self._get_ts(
            self._torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)),
            self._model,
            sampling_rate=SAMPLE_RATE,
            threshold=self._threshold,
            min_silence_duration_ms=self._min_silence_ms,
            speech_pad_ms=self._speech_pad_ms,
            min_speech_duration_ms=self._min_speech_ms,
            return_seconds=True,
        )
        return [Span(float(s["start"]), float(s["end"])) for s in stamps]


def level_dbfs(audio: np.ndarray, percentile: float = 95.0, frame_ms: int = 30) -> float:
    """Loudness of the loud part of ``audio``: a high percentile of 30 ms frame RMS, dBFS."""
    frame = int(SAMPLE_RATE * frame_ms / 1000)
    n = audio.size // frame
    if n == 0:
        return -120.0
    frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
    rms = float(np.percentile(np.sqrt((frames**2).mean(axis=1)), percentile))
    return 20.0 * float(np.log10(max(rms, 1e-6)))


class GainNormalizedVAD:
    """Boost quiet audio before handing it to another VAD (the stored audio is untouched).

    Far-field and wrist-worn recordings can be 20-30 dB quieter than a close mic, and
    Silero then misses much of the speech. The loud part of the chunk (95th percentile of
    frame RMS) is raised to ``target_dbfs``, by at most ``max_gain_db``; loud audio is
    never turned down.
    """

    def __init__(self, inner: VAD, target_dbfs: float = -20.0,
                 max_gain_db: float = 30.0) -> None:
        self.inner = inner
        self.target_dbfs = target_dbfs
        self.max_gain_db = max_gain_db

    def gain_db(self, audio: np.ndarray) -> float:
        return min(self.max_gain_db, max(0.0, self.target_dbfs - level_dbfs(audio)))

    def detect(self, audio: np.ndarray) -> list[Span]:
        g = self.gain_db(audio)
        if g > 0:
            audio = np.clip(audio * np.float32(10 ** (g / 20)), -1.0, 1.0).astype(np.float32)
        return self.inner.detect(audio)


class EnergyVAD:
    """RMS threshold over 30 ms frames. Not for production use."""

    def __init__(self, threshold: float = 0.02, frame_ms: int = 30, min_gap: float = 0.5) -> None:
        self.threshold = threshold
        self.frame = int(SAMPLE_RATE * frame_ms / 1000)
        self.min_gap = min_gap

    def detect(self, audio: np.ndarray) -> list[Span]:
        n = audio.size // self.frame
        if n == 0:
            return []
        frames = audio[: n * self.frame].reshape(n, self.frame)
        active = np.sqrt((frames**2).mean(axis=1)) > self.threshold
        spans: list[Span] = []
        start = None
        sec = self.frame / SAMPLE_RATE
        for i, on in enumerate(active):
            if on and start is None:
                start = i * sec
            elif not on and start is not None:
                spans.append(Span(start, i * sec))
                start = None
        if start is not None:
            spans.append(Span(start, n * sec))
        return merge_spans(spans, self.min_gap)


def merge_spans(spans: list[Span], max_gap: float) -> list[Span]:
    """Merge spans separated by less than ``max_gap`` seconds."""
    out: list[Span] = []
    for s in sorted(spans, key=lambda s: s.start):
        if out and s.start - out[-1].end < max_gap:
            out[-1] = Span(out[-1].start, max(out[-1].end, s.end))
        else:
            out.append(s)
    return out


def make_vad(backend: str, settings: Settings | None = None) -> VAD:
    """Build the VAD; Silero options come from ``settings`` (CONVO_VAD_*) when given."""
    if backend == "silero":
        if settings is None:
            return GainNormalizedVAD(SileroVAD())
        vad: VAD = SileroVAD(threshold=settings.vad_threshold,
                             min_silence_ms=settings.vad_min_silence_ms,
                             speech_pad_ms=settings.vad_speech_pad_ms)
        if settings.vad_max_gain_db > 0:
            vad = GainNormalizedVAD(vad, settings.vad_target_dbfs, settings.vad_max_gain_db)
        return vad
    if backend == "energy":
        return EnergyVAD()
    raise ValueError(f"unknown VAD backend {backend!r}")
