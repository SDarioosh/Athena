"""The batch lookups list_facts and list_people use give the same answers as the
one-row lookups they replace. Fictional data."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import pytest

from athena.db.carry_over import _chain_end
from athena.db.facts import QUOTE_CHARS, _chain_end_in
from athena.db.matching import _cited_line
from athena.db.people import _person_id, _person_ids
from tests.unit.mcp.test_query_counts import seed

NAMES = ["Alice", "alice", "ALICE", " Alice ", "Bob", "bob", "Robert", "Dave", "dave", "Me",
         "me", "user", "Erin", "the office", "", " ", "Nobody", "Émile", "émile", "ÉMILE",
         "Zoë", "zoë", "Speaker 1", "Al", "Alicia", "Ann", "ann", "Loop", "loop"]


@pytest.fixture
def store(pipeline: Any) -> Any:
    seed(pipeline.store)
    s = pipeline.store
    now = time.time()
    with s.tx() as c:
        # names SQLite's NOCASE does not fold (only ASCII letters are), an alias that is
        # another person's name, a two-step merge chain and a merge loop
        for name in ("Émile", "Zoë", "Ann", "Ann2", "Ann3", "Loop", "Loop2"):
            c.execute("INSERT INTO people (name, created_at) VALUES (?, ?)", (name, now))
        ids = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM people")}
        c.execute("INSERT INTO people_aliases VALUES (?, 'zoë')", (ids["Ann"],))
        c.execute("INSERT INTO people_aliases VALUES (?, 'Alice')", (ids["Ann"],))
        c.execute("UPDATE people SET merged_into = ? WHERE id = ?", (ids["Ann2"], ids["Ann"]))
        c.execute("UPDATE people SET merged_into = ? WHERE id = ?", (ids["Ann3"], ids["Ann2"]))
        c.execute("UPDATE people SET merged_into = ? WHERE id = ?", (ids["Loop2"], ids["Loop"]))
        c.execute("UPDATE people SET merged_into = ? WHERE id = ?", (ids["Loop"], ids["Loop2"]))
    return s


def test_person_ids_match_person_id(store: Any) -> None:
    with store._lock:
        one = {n: _person_id(store._conn, n) for n in NAMES}
        many = _person_ids(store._conn, NAMES)
    assert many == one
    assert one["Alice"] is not None and one["Émile"] != one["émile"]


@pytest.mark.parametrize("owners", [(), ("Me", "me", "i", "user"), ("Sam", "me"), ("Nobody",)])
def test_store_person_ids_match_person_id(store: Any, owners: tuple[str, ...]) -> None:
    assert store.person_ids(NAMES, owners) == {n: store.person_id(n, owners) for n in NAMES}


def test_person_ids_in_large_batches(store: Any) -> None:
    names = [f"Nobody {i}" for i in range(900)] + NAMES
    assert store.person_ids(names) == {n: store.person_id(n) for n in names}


def test_sources_and_history_match(store: Any) -> None:
    ids = [r["id"] for r in store._q("SELECT id FROM facts")] + [999_999]
    assert store.fact_sources_for(ids) == {i: store.fact_sources(i) for i in ids}
    assert store.fact_history_for(ids) == {i: store.fact_history(i) for i in ids}
    assert any(store.fact_sources_for(ids).values())
    assert any(store.fact_history_for(ids).values())


def test_chain_ends_match(store: Any) -> None:
    with store.tx() as c:  # a loop, as _chain_end guards against
        a, b = (r[0] for r in c.execute("SELECT id FROM facts WHERE status = 'current'"
                                        " ORDER BY id LIMIT 2"))
        c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                  (b, a))
        c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                  (a, b))
    replaced = {r["id"]: r["superseded_by"] for r in store._q(
        "SELECT id, superseded_by FROM facts WHERE status = 'superseded'")}
    with store._lock:
        for r in store._q("SELECT id FROM facts"):
            assert _chain_end_in(replaced, r["id"]) == _chain_end(store._conn, r["id"])


def _old_fact_quotes(store: Any, facts: list[dict[str, Any]]) -> dict[int, str | None]:
    """Store.fact_quotes as it was before it read in batches."""
    lines: dict[str, list[sqlite3.Row]] = {}

    def rows(conv_id: str) -> list[sqlite3.Row]:
        if conv_id not in lines:
            with store._lock:
                lines[conv_id] = store._conn.execute(
                    "SELECT id, session_id, started_at, ended_at, erased, text"
                    " FROM segments WHERE conversation_id = ? ORDER BY started_at",
                    (conv_id,)).fetchall()
        return lines[conv_id]

    out: dict[int, str | None] = {}
    for f in facts:
        said = [(f["conversation_id"], f.get("source_at"))]
        if not rows(f["conversation_id"]):
            said += [(r["conversation_id"], r["source_at"]) for r in store._q(
                "SELECT s.conversation_id, s.source_at FROM fact_sources s"
                " JOIN conversations c ON c.id = s.conversation_id WHERE s.fact_id = ?"
                " ORDER BY c.started_at DESC", (f["id"],))]
        line = None
        for conv_id, at in said:
            if at is None or not rows(conv_id):
                continue
            hit = _cited_line(rows(conv_id), at, f.get("fact") or "")
            if hit is not None and not hit["erased"]:
                line = " ".join(str(hit["text"] or "").split())[:QUOTE_CHARS] or None
            break
        out[f["id"]] = line
    return out


def test_fact_quotes_match(store: Any) -> None:
    facts = store._q("SELECT * FROM facts ORDER BY id")
    pruned = store._q("SELECT id FROM conversations WHERE transcript_pruned_at IS NOT NULL")
    assert pruned
    got = store.fact_quotes(facts)
    assert got == _old_fact_quotes(store, facts)
    assert sum(q is not None for q in got.values()) > len(facts) // 2
