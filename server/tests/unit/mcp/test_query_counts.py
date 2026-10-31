"""How many SQL statements the list tools run, on a seeded fictional store.

list_facts looked up each fact's sources, history and person one query at a time (two
queries per fact in the text format, one or more per fact in JSON, plus one per replaced
fact and per conversation); now it reads each kind once. The results are pinned by hash
(on a larger store than the contract snapshots; the hashes were taken on the code before
batching) so fewer queries are shown to give the same output.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from athena.db import conversations as store_module
from athena.domain import Segment
from athena.mcp_server import build_mcp
from tests.unit.mcp.test_mcp import call

T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC
DAY = 86400.0
CONVERSATIONS = 40
SUBJECTS = ("Alice", "Bob", "Me", "the office", "Dave", "Robert", "Erin", "user")


def seed(store: Any) -> None:
    """40 conversations, two facts each; conversations 10-29 say a fact of 0-9 again
    ("same": three sources on three days), 30-39 replace one of 0-9 ("updates"); Bob is
    renamed (an alias), Dave merged into Alice; the first five transcripts are pruned
    (quotes then come from a repeat)."""
    v = np.zeros(16, dtype=np.float32)
    v[0] = 1.0
    store.put_speaker("Me", v, is_owner=True)
    ids: list[tuple[int, int]] = []
    for k in range(CONVERSATIONS):
        name, start = f"c{k:02d}", T0 + k * DAY / 2
        a, b = SUBJECTS[k % len(SUBJECTS)], SUBJECTS[(k + 3) % len(SUBJECTS)]
        fa, fb = f"{a} likes topic {k % 20}", f"{b} plans trip {k % 10}"
        store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                        duration=30, codec="wav", audio_path=None, speech=[])
        store.replace_segments(name, "omi", [
            Segment(start, start + 4, f"So, {fa.lower()}", "Me", True),
            Segment(start + 10, start + 14, f"And {fb.lower()}", "Bob" if k % 2 else "Dave"),
        ], [(start, start + 30)])
        conv = store.save_conversation(None, "omi",
                                       [s.id for s in store.session_segments(name)])
        got = store.save_summary(conv, {
            "title": f"Talk {k}", "summary": f"{fa}. {fb}.", "key_points": [],
            "topics": [], "people": ["Alice", "Bob", "Dave"][: 1 + k % 3],
            "action_items": [],
            "facts": [{"subject": a, "fact": fa, "source_at": start + 1},
                      {"subject": b, "fact": fb, "source_at": start + 11}]})
        assert got is not None
        ids.append((got[0], got[1]))
        if 10 <= k < 30:
            store.link_fact(got[0], "same", ids[k % 10][0])
        if k >= 30:
            store.link_fact(got[1], "updates", ids[k - 30][1])
    assert store.prune_transcripts(T0 + 5 * DAY / 2 - 1)["conversations"] == 5
    store.rename_everywhere("Bob", "Robert")
    store.rename_everywhere("Dave", "Alice")


@contextmanager
def counted(store: Any) -> Iterator[list[str]]:
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        store._conn.set_trace_callback(None)


# (arguments, sha256 of the result, statements before batching, the bound now)
CASES: list[tuple[dict[str, Any], str, int, int]] = [
    ({"limit": 200}, "a13f9e2410187ade7e42ddbf36e6abd1f5166358d1b7564235e01112997bc92e",
     125, 8),
    ({"limit": 200, "format": "json"},
     "d37899d9f07d0ad87980f417d948653723457bc739a66bc20a41f6b176143cc1", 97, 11),
    ({"confirmed": True}, "920e9ce05348e044312967bb8bd7c4b094bba919a6cc5082ca467996f9d4ab7a",
     46, 9),
    ({"subject": "Alice", "format": "json"},
     "ba2d551eee26a617c9c712373aadc7cc2bf6dfbb8658fc2405fb2c4b22430f3e", 26, 12),
]
PEOPLE = ("31a9ba4c04f0c0144a44a67e5bdd7a89266f4d4b4568a05d5edf54b6b2acac6c", 19, 9)


@pytest.fixture
def seeded(pipeline: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(store_module, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=f"{0xFAC70000 + next(counter):032x}")))
    seed(pipeline.store)
    return pipeline


@pytest.mark.parametrize("args, digest, before, bound", CASES)
def test_list_facts_queries(seeded: Any, args: dict[str, Any], digest: str,
                            before: int, bound: int) -> None:
    mcp = build_mcp(seeded.store, seeded.settings)
    with counted(seeded.store) as statements:
        out = call(mcp, "list_facts", **args)
    assert hashlib.sha256(out.encode()).hexdigest() == digest
    assert len(statements) <= bound < before


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_list_facts_queries_do_not_grow_with_the_facts(seeded: Any, fmt: str) -> None:
    mcp = build_mcp(seeded.store, seeded.settings)
    counts = []
    for limit in (3, 200):
        with counted(seeded.store) as statements:
            call(mcp, "list_facts", limit=limit, format=fmt)
        counts.append(len(statements))
    # JSON: two more when a fact's own transcript was pruned (its repeats and their lines)
    assert counts[0] <= counts[1] <= counts[0] + (2 if fmt == "json" else 0)


def test_list_people_queries(seeded: Any) -> None:
    mcp = build_mcp(seeded.store, seeded.settings)
    with counted(seeded.store) as statements:
        out = call(mcp, "list_people", format="json")
    assert hashlib.sha256(out.encode()).hexdigest() == PEOPLE[0]
    assert len(statements) <= PEOPLE[2] < PEOPLE[1]
