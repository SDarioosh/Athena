"""Cutting a chat's messages into threads, and which old thread each new one continues.

Pure functions, so threading is the same whatever order the messages arrived in
(docs/messages.md, "Threads").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

# A long thread starts a new one at a shorter gap, and a thread never grows past HARD_MAX:
# a chat that never pauses still gets summaries of a size the model handles.
SOFT_MAX = 150
SOFT_GAP_SECONDS = 30 * 60
HARD_MAX = 300


@dataclass(frozen=True)
class Line:
    """One message's line, as threading sees it."""
    segment_id: int
    at: float
    thread: str | None  # the conversation it is in now; None for a new message


def split(lines: Sequence[Line], gap: float, soft_max: int = SOFT_MAX,
          soft_gap: float = SOFT_GAP_SECONDS, hard_max: int = HARD_MAX) -> list[list[Line]]:
    """``lines`` (in time order) cut into threads: at a gap over ``gap``, at a gap over
    ``soft_gap`` once a thread has ``soft_max`` lines, and at ``hard_max`` lines."""
    threads: list[list[Line]] = []
    for line in lines:
        cur = threads[-1] if threads else None
        if cur is None:
            threads.append([line])
            continue
        pause = line.at - cur[-1].at
        if pause > gap or len(cur) >= hard_max or (len(cur) >= soft_max and pause > soft_gap):
            threads.append([line])
        else:
            cur.append(line)
    return threads


@dataclass(frozen=True)
class Plan:
    """What threading a stretch of a chat again changes.

    ``ids[i]`` is the conversation thread ``i`` continues (an old id), or None for a new
    conversation. ``absorb`` maps each old thread no new thread continues to the one that
    took most of its lines; it is merged into it, so its facts and to-dos move along.
    """
    ids: list[str | None]
    absorb: dict[str, str]


def plan(threads: Sequence[Sequence[Line]]) -> Plan:
    """Give each new thread the old conversation it shares the most lines with, each old
    one at most once, biggest overlaps first (ties: the earlier thread). An old thread left
    over is merged into the thread holding most of its lines: that thread already has an
    id, or it would have taken this one."""
    shares: list[tuple[int, int, int, str]] = []  # (-lines, thread, first line, old id)
    for i, t in enumerate(threads):
        first: dict[str, int] = {}
        for pos, x in enumerate(t):
            if x.thread:
                first.setdefault(x.thread, pos)
        counts = Counter(x.thread for x in t if x.thread)
        shares += [(-n, i, first[old], old) for old, n in counts.items()]
    shares.sort()
    ids: list[str | None] = [None] * len(threads)
    taken: set[str] = set()
    for _, i, _, old in shares:
        if ids[i] is None and old not in taken:
            ids[i] = old
            taken.add(old)
    absorb: dict[str, str] = {}
    for _, i, _, old in shares:
        into = ids[i]
        if old not in taken and old not in absorb and into is not None:
            absorb[old] = into
    return Plan(ids, absorb)
