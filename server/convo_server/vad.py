"""Voice activity detection.

``SileroVAD`` is the production backend. ``EnergyVAD`` is a crude RMS-threshold
detector that exists only for tests / machines without torch; select it
explicitly with ``CONVO_VAD_BACKEND=energy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

from .audio import SAMPLE_RATE, bounded_gain

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
                 speech_pad_ms: int = 30) -> None:
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
        self._speech_pad_ms = speech_pad_ms  # Silero's own default is 30

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
            return_seconds=True,
        )
        return [Span(float(s["start"]), float(s["end"])) for s in stamps]


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


class GainedVAD:
    """Runs ``inner`` on a gained copy of each buffer (docs/omi-audio-quality.md §4.2).

    Quiet far-field speech that Silero would score as silence is scaled toward a
    ``target`` peak (at most ``max_gain`` times, never attenuated) before scoring. The
    caller's array, and so the stored audio, is left as it was. Each ``detect`` call
    (one 30 s chunk at ingest) gets its own gain.
    """

    def __init__(self, inner: VAD, max_gain: float = 4.0, target: float = 0.8) -> None:
        self.inner = inner
        self.max_gain = max_gain
        self.target = target

    def detect(self, audio: np.ndarray) -> list[Span]:
        gained, _ = bounded_gain(audio, target=self.target, max_gain=self.max_gain)
        return self.inner.detect(gained)


def make_vad(backend: str, settings: Settings | None = None) -> VAD:
    """Build the VAD. With ``settings``, apply its threshold, padding and VAD gain."""
    if backend == "silero":
        vad: VAD = (SileroVAD(threshold=settings.vad_threshold,
                              speech_pad_ms=settings.vad_speech_pad_ms)
                    if settings is not None else SileroVAD())
    elif backend == "energy":
        vad = EnergyVAD()
    else:
        raise ValueError(f"unknown VAD backend {backend!r}")
    if settings is not None and settings.vad_agc_max_gain > 1.0:
        vad = GainedVAD(vad, settings.vad_agc_max_gain, settings.vad_agc_target)
    return vad
