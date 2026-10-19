"""Cut a run of audio into the clips that go to whisper.

Speech regions (VAD spans, already merged) are padded and packed, in order, into windows
of at most ``max_seconds``. whisper is trained on 30 s windows: a clip with a few
sentences in it decodes better than one 2 s phrase on its own. Silence between two
regions in a window is shortened to ``max_gap`` seconds (half of it kept after the first
region, half before the next), so a window is a list of slices of the run audio played
back to back. ``Window.to_run`` maps a time in the clip back to run time.

``max_seconds <= 0`` sends every padded region alone (the old behaviour). A region
longer than ``max_seconds`` gets a window of its own; whisper-server splits it itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE
from .transcribe import TranscriptPiece
from .vad import Span


@dataclass(frozen=True)
class Window:
    parts: tuple[tuple[float, float], ...]  # run-time slices (s), back to back in the clip

    @property
    def duration(self) -> float:
        return sum(b - a for a, b in self.parts)

    def extract(self, audio: np.ndarray) -> np.ndarray:
        slices = [audio[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)] for a, b in self.parts]
        return slices[0] if len(slices) == 1 else np.concatenate(slices)

    def to_run(self, t: float, *, end: bool = False) -> float:
        """Run time of clip time ``t``. At a cut, a start maps after it, an end before it."""
        t = min(max(t, 0.0), self.duration)
        off = 0.0
        for i, (a, b) in enumerate(self.parts):
            length = b - a
            last = i == len(self.parts) - 1
            if last or t < off + length or (end and t <= off + length):
                return a + min(t - off, length)
            off += length
        raise AssertionError("unreachable")

    def part_of(self, t: float) -> int:
        """Index of the part clip time ``t`` falls in."""
        off = 0.0
        for i, (a, b) in enumerate(self.parts):
            off += b - a
            if t < off:
                return i
        return len(self.parts) - 1

    def split(self, piece: TranscriptPiece) -> list[TranscriptPiece]:
        """Split a piece that runs over a cut, by its word times, one piece per part.

        Without word times (or within one part) the piece is returned as is.
        """
        if len(self.parts) == 1 or not piece.words or (
                self.part_of(piece.start) == self.part_of(max(piece.start, piece.end - 1e-3))):
            return [piece]
        groups: dict[int, list[tuple[float, float, str]]] = {}
        for w in piece.words:
            groups.setdefault(self.part_of((w[0] + w[1]) / 2), []).append(w)
        out = []
        for i in sorted(groups):
            ws = groups[i]
            text = "".join(w[2] for w in ws).strip()
            if text:
                out.append(TranscriptPiece(ws[0][0], max(ws[-1][1], ws[0][0]), text, tuple(ws)))
        return out or [piece]


def plan_windows(regions: list[Span], run_seconds: float, *, pad: float, max_seconds: float,
                 max_gap: float) -> list[Window]:
    """Pad ``regions`` (sorted, within the run) and pack them into windows."""
    padded: list[list[float]] = []
    for r in sorted(regions, key=lambda r: r.start):
        a, b = max(0.0, r.start - pad), min(run_seconds, r.end + pad)
        if b <= a:
            continue
        if padded and a <= padded[-1][1]:  # padding met the neighbour: one interval
            padded[-1][1] = max(padded[-1][1], b)
        else:
            padded.append([a, b])
    if max_seconds <= 0:
        return [Window(((a, b),)) for a, b in padded]

    windows: list[Window] = []
    parts: list[list[float]] = []
    length = 0.0
    for a, b in padded:
        if parts:
            gap = a - parts[-1][1]
            kept = min(gap, max(max_gap, 0.0))
            if length + kept + (b - a) <= max_seconds:
                if gap <= kept:  # short silence: keep it, one contiguous slice
                    parts[-1][1] = b
                else:
                    parts[-1][1] += kept / 2
                    parts.append([a - kept / 2, b])
                length += kept + (b - a)
                continue
            windows.append(Window(tuple((x, y) for x, y in parts)))
        parts, length = [[a, b]], b - a
    if parts:
        windows.append(Window(tuple((x, y) for x, y in parts)))
    return windows


def tail_words(text: str, n: int) -> str:
    """The last ``n`` words of ``text`` (whisper prompt carry-over)."""
    if n <= 0:
        return ""
    return " ".join(text.split()[-n:])
