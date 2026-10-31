"""Group transcript segments into conversations separated by silence gaps."""

from __future__ import annotations

from collections.abc import Sequence

from .domain import Segment


def split_on_gaps(segments: Sequence[Segment], gap_seconds: float) -> list[list[Segment]]:
    """Split time-ordered segments wherever speech pauses for more than ``gap_seconds``."""
    groups: list[list[Segment]] = []
    last_end: float | None = None
    for seg in sorted(segments, key=lambda s: s.started_at):
        if last_end is None or seg.started_at - last_end > gap_seconds:
            groups.append([])
        groups[-1].append(seg)
        last_end = seg.ended_at if last_end is None else max(last_end, seg.ended_at)
    return groups
