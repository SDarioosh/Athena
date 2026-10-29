"""retire_fact: the owner forgets a fact on the hub's review page, and it stays forgotten
(list_facts, search, re-summaries, repeats). Also list_facts' per-row ``confirmed``.
Fictional data."""

from __future__ import annotations

import json

from test_confirmed_facts import DAY, HOUR, T0, said_at, say
from test_mcp import call

from athena.mcp_server import build_mcp


def facts_json(pipeline, **args) -> list[dict]:
    return json.loads(call(build_mcp(pipeline.store, pipeline.settings), "list_facts",
                           format="json", **args))["facts"]


def retire(pipeline, fact_id: int, reason: str = "") -> str:
    return call(build_mcp(pipeline.store, pipeline.settings), "retire_fact",
                fact_id=fact_id, reason=reason)


def summary(fact: str | None, subject: str = "Alice") -> dict:
    return {"title": "t", "summary": "s", "key_points": [], "topics": [], "people": [],
            "action_items": [],
            "facts": [{"subject": subject, "fact": fact, "source_at": T0 + 2}] if fact else []}


def test_retire_hides_the_fact_everywhere(pipeline):
    store = pipeline.store
    conv, fid = say(store, "c0", T0, "Alice", "Alice is vegetarian")
    _, other = say(store, "c1", T0 + HOUR, "Bob", "Bob plays the cello")
    assert retire(pipeline, fid, "wrong") == f"Fact #{fid} retired."
    row = store._one("SELECT * FROM facts WHERE id = ?", (fid,))
    assert row["status"] == "retired" and row["retired_reason"] == "wrong"
    assert row["retired_at"] is not None
    assert [f["id"] for f in facts_json(pipeline)] == [other]
    text = call(build_mcp(store, pipeline.settings), "list_facts")
    assert "vegetarian" not in text and "cello" in text
    assert store.list_facts(query="vegetarian") == []
    assert store.list_facts(subject="Alice") == []
    assert not [h for h in store.fts_search("vegetarian") if h["kind"] == "fact"]
    assert store.get_conversation(conv)["facts"] == []
    assert "Fact [" not in call(build_mcp(store, pipeline.settings), "get_conversation",
                                conversation_id=conv)


def test_retire_is_idempotent_and_unknown_ids_are_reported(pipeline):
    _, fid = say(pipeline.store, "c0", T0, "Alice", "Alice is vegetarian")
    assert retire(pipeline, fid) == f"Fact #{fid} retired."
    assert retire(pipeline, fid) == f"Fact #{fid} was already retired."
    assert retire(pipeline, 9999) == "No fact #9999."


def test_retiring_a_fact_keeps_what_it_replaced_hidden(pipeline):
    store = pipeline.store
    _, old = say(store, "a", T0, "Bob", "Bob lives in Paris")
    _, new = say(store, "b", T0 + DAY, "Bob", "Bob lives in Berlin")
    store.link_fact(new, "updates", old)
    retire(pipeline, new)
    assert facts_json(pipeline) == []
    assert store.list_facts(query="Paris") == []


def test_a_repeat_linked_same_to_a_retired_fact_stays_hidden(pipeline):
    store = pipeline.store
    _, fid = say(store, "c0", T0, "Alice", "Alice is vegetarian")
    retire(pipeline, fid)
    conv, again = say(store, "c1", T0 + DAY, "Alice", "Alice is vegetarian")
    # the linker is offered the retired fact, so it can absorb the repeat
    known = store.related_facts(conv, store.facts_by_id([again]))
    assert fid in [k["id"] for k in known]
    store.link_fact(again, "same", fid)
    assert facts_json(pipeline) == []
    assert store.facts_by_id([again]) == []
    assert [s["conversation_id"] for s in store.fact_sources(fid)] == [conv]
    assert store._one("SELECT status FROM facts WHERE id = ?", (fid,))["status"] == "retired"


def test_retired_facts_are_offered_to_the_linker_by_words(pipeline):
    store = pipeline.store
    _, fid = say(store, "c0", T0, "Alice", "Alice is vegetarian")
    retire(pipeline, fid)
    conv, again = say(store, "c1", T0 + DAY, "Al", "She is strictly vegetarian")
    assert fid in [k["id"] for k in store.related_facts(conv, store.facts_by_id([again]))]
    _, unrelated = say(store, "c2", T0 + 2 * DAY, "Bob", "Bob plays the cello")
    assert fid not in [k["id"] for k in store.related_facts("c2", store.facts_by_id([unrelated]))]


def test_a_newer_fact_updating_a_retired_one_is_shown(pipeline):
    store = pipeline.store
    _, old = say(store, "a", T0, "Bob", "Bob lives in Paris")
    retire(pipeline, old)
    _, new = say(store, "b", T0 + DAY, "Bob", "Bob lives in Berlin")
    store.link_fact(new, "updates", old)
    assert [f["id"] for f in facts_json(pipeline)] == [new]
    assert store._one("SELECT status FROM facts WHERE id = ?", (old,))["status"] == "retired"
    assert store.fact_history(new) == []  # a forgotten fact is not shown as history


def test_resummarizing_the_conversation_does_not_bring_a_retired_fact_back(pipeline):
    store = pipeline.store
    conv, fid = say(store, "c0", T0, "Alice", "Alice is vegetarian")
    retire(pipeline, fid, "private")
    # a speaker rename redoes the summary: the same fact comes back with a new row
    assert store.save_summary(conv, summary("Alice is vegetarian")) == []
    assert facts_json(pipeline) == []
    (row,) = store._q("SELECT * FROM facts WHERE conversation_id = ?", (conv,))
    assert row["status"] == "retired" and row["retired_reason"] == "private"
    assert not [h for h in store.fts_search("vegetarian") if h["kind"] == "fact"]
    # a re-summary that misses it, then one that says it again: still retired
    store.save_summary(conv, summary(None))
    assert store.save_summary(conv, summary("Alice is vegetarian.")) == []
    assert facts_json(pipeline) == []
    # an unrelated new fact in that conversation is shown
    new_ids = store.save_summary(conv, {**summary("Alice is vegetarian"), "facts": [
        {"subject": "Alice", "fact": "Alice is vegetarian", "source_at": T0 + 2},
        {"subject": "Alice", "fact": "Alice runs marathons", "source_at": T0 + 3}]})
    assert [f["fact"] for f in facts_json(pipeline)] == ["Alice runs marathons"]
    assert len(new_ids) == 1


def test_resummarizing_a_conversation_that_repeated_a_retired_fact(pipeline):
    store = pipeline.store
    _, fid = say(store, "c0", T0, "Alice", "Alice is vegetarian")
    conv, again = say(store, "c1", T0 + DAY, "Alice", "Alice is vegetarian")
    store.link_fact(again, "same", fid)
    retire(pipeline, fid)
    assert store.save_summary(conv, summary("Alice is vegetarian")) == []
    assert facts_json(pipeline) == []


def test_json_rows_say_whether_they_are_confirmed(pipeline):
    store = pipeline.store
    confirmed = said_at(store, [T0, T0 + HOUR, T0 + DAY])
    _, once = say(store, "once", T0 + 2 * DAY, "Bob", "Bob plays the cello")
    rows = {f["id"]: f["confirmed"] for f in facts_json(pipeline)}
    assert rows == {confirmed: True, once: False}
    assert [f["id"] for f in facts_json(pipeline, confirmed=True)] == [confirmed]
    loose = pipeline.settings.model_copy(update={"fact_confirm_sources": 1,
                                                 "fact_confirm_days": 1})
    out = json.loads(call(build_mcp(store, loose), "list_facts", format="json"))
    assert all(f["confirmed"] for f in out["facts"])
