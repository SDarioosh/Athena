"""Threading: how a chat's messages are cut into threads (texts/threads.py)."""

from __future__ import annotations

from athena.texts.threads import HARD_MAX, SOFT_MAX, Line, plan, split

H = 3600.0


def lines(times: list[float], threads: list[str | None] | None = None) -> list[Line]:
    threads = threads or [None] * len(times)
    return [Line(i, t, c) for i, (t, c) in enumerate(zip(times, threads, strict=True))]


def test_split_at_gaps_over_the_setting():
    got = split(lines([0, 60, 5 * H, 5 * H + 30, 12 * H]), gap=6 * H)
    assert [[x.at for x in t] for t in got] == [[0, 60, 5 * H, 5 * H + 30], [12 * H]]


def test_a_long_thread_splits_at_a_shorter_gap_then_at_the_hard_cap():
    times = [i * 60.0 for i in range(SOFT_MAX)] + [SOFT_MAX * 60.0 + 31 * 60]
    got = split(lines(times), gap=6 * H)
    assert [len(t) for t in got] == [SOFT_MAX, 1]  # 31 min pause after 150 lines
    nonstop = split(lines([i * 10.0 for i in range(HARD_MAX + 5)]), gap=6 * H)
    assert [len(t) for t in nonstop] == [HARD_MAX, 5]


def test_plan_keeps_old_ids_and_merges_a_thread_a_backfill_joins():
    # two old threads 8 h apart; a backfilled message in between joins them (gap 6 h)
    ls = lines([0, 60, 4 * H, 8 * H, 8 * H + 60], ["a", "a", None, "b", "b"])
    groups = split(ls, gap=6 * H)
    assert len(groups) == 1
    p = plan(groups)
    assert p.ids == ["a"] and p.absorb == {"b": "a"}  # tie: the earlier thread wins


def test_plan_gives_a_new_thread_no_id_and_each_old_id_once():
    ls = lines([0, 60, 10 * H, 10 * H + 60], ["a", "a", "a", None])
    p = plan(split(ls, gap=6 * H))
    assert p.ids == ["a", None] and p.absorb == {}
