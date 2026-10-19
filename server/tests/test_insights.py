"""Focused summaries: discard check, templates, due dates, facts merged over time."""

from __future__ import annotations

import numpy as np
from conftest import fake_llm_transport, silence, tone
from test_mcp import T0, call, ingest

from convo_server.llm import LLMClient, Summarizer
from convo_server.mcp_server import build_mcp


def use_llm(pipeline, log=None, **fake):
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(log=log, **fake))
    pipeline.summarizer = Summarizer(llm, "Me", timezone="Europe/Berlin")


def record(pipeline, session="s1", start=T0):
    ingest(pipeline, np.concatenate([tone(200, 4), silence(4), tone(600, 4), silence(18)]),
           session=session, start=start)
    pipeline.end_session(session)
    pipeline.process_pending()
    return pipeline.store.list_conversations(since=start - 1, until=start + 600)[0]["id"]


def schemas(log):
    return [b["response_format"]["json_schema"]["name"] for b in log]


def test_focused_calls_with_template_and_due_dates(pipeline):
    log: list = []
    use_llm(pipeline, log, triage={"discard": False, "reason": "none", "kind": "meeting"})
    conv_id = record(pipeline)
    assert schemas(log) == ["Triage", "SummaryNotes", "ActionItemList", "FactList"]
    systems = [b["messages"][0]["content"] for b in log]
    assert "This is a meeting" in systems[1] and "decisions" in systems[1]
    assert "Thursday 2025-10-09" in systems[2]  # the date due dates are counted from
    conv = pipeline.store.get_conversation(conv_id)
    assert conv["kind"] == "meeting" and conv["status"] == "summarized"
    item = conv["action_items"][0]
    assert item["due_date"] == "2025-10-10" and item["due"] == "Friday"
    mcp = build_mcp(pipeline.store, pipeline.settings)
    assert "due 2025-10-10 (Friday)" in call(mcp, "list_action_items")
    assert "type meeting" in call(mcp, "get_conversation", conversation_id=conv_id)


def test_bad_due_dates_are_dropped(pipeline):
    summary = {"title": "t", "summary": "s", "key_points": [], "people": [], "topics": [],
               "facts": [], "action_items": [
                   {"text": "a", "due": "Friday", "due_date": "Friday", "at_seconds": 1},
                   {"text": "b", "due": "soon", "due_date": "2026-02-30", "at_seconds": 1}]}
    use_llm(pipeline, summary=summary)
    conv = pipeline.store.get_conversation(record(pipeline))
    assert [a["due_date"] for a in conv["action_items"]] == [None, None]


def test_tv_is_discarded_but_transcript_kept(pipeline):
    log: list = []
    use_llm(pipeline, log, triage={"discard": True, "reason": "tv_or_media", "kind": "other"})
    conv_id = record(pipeline)
    assert schemas(log) == ["Triage"]  # nothing else is asked
    conv = pipeline.store.get_conversation(conv_id)
    assert conv["status"] == "discarded" and conv["discard_reason"] == "tv_or_media"
    assert conv["action_items"] == [] and conv["facts"] == []
    assert pipeline.make_digest("2025-10-09") is None
    mcp = build_mcp(pipeline.store, pipeline.settings)
    assert "looked like tv_or_media" in call(mcp, "search_conversations", query="words")
    assert "words at 600 hz" in call(mcp, "search_transcripts", query="words")
    # "when in doubt keep it": discard without a reason is not a discard
    use_llm(pipeline, triage={"discard": True, "reason": "none", "kind": "casual"})
    assert pipeline.summarize_conversation(conv_id)
    assert pipeline.store.get_conversation(conv_id)["status"] == "summarized"


def test_too_short_is_discarded_without_a_model_call(pipeline):
    from convo_server.store import Segment

    log: list = []
    use_llm(pipeline, log)
    out = pipeline.summarizer.summarize([Segment(0, 1, "Okay.", "Speaker 1")])
    assert out == {"discard": True, "discard_reason": "too_short", "kind": "other"}
    assert log == []


def fact_summary(fact):
    return {"title": "t", "summary": "s", "key_points": [], "people": ["Alice"],
            "topics": [], "action_items": [],
            "facts": [{"subject": "Q4 budget", "fact": fact, "at_seconds": 1}]}


def test_newer_fact_replaces_older_and_keeps_links(pipeline):
    store = pipeline.store
    use_llm(pipeline, summary=fact_summary("The Q4 budget cut is fifteen percent."))
    first = record(pipeline)
    old_id = store.get_conversation(first)["facts"][0]["id"]

    log: list = []
    use_llm(pipeline, log, summary=fact_summary("The Q4 budget cut is ten percent."),
            links=[{"new": 0, "relation": "updates", "existing": old_id}])
    second = record(pipeline, session="s2", start=T0 + 86400)
    assert schemas(log)[-1] == "FactLinks"
    assert f'"id": {old_id}' in log[-1]["messages"][1]["content"]

    facts = store.list_facts()
    assert [f["fact"] for f in facts] == ["The Q4 budget cut is ten percent."]
    # searching for the old value finds the fact that replaced it
    assert [f["fact"] for f in store.list_facts(query="fifteen")] == [facts[0]["fact"]]
    mcp = build_mcp(store, pipeline.settings)
    out = call(mcp, "list_facts", subject="budget")
    assert "ten percent" in out and "replaces: The Q4 budget cut is fifteen percent." in out
    assert out.count("/view/") == 2  # both transcripts stay linked

    # re-summarizing the newer conversation restores the old fact until linked again
    use_llm(pipeline, summary=fact_summary("Nothing about the budget."), links=[])
    pipeline.summarize_conversation(second)
    assert {f["fact"] for f in store.list_facts()} == {
        "The Q4 budget cut is fifteen percent.", "Nothing about the budget."}


def test_older_conversation_cannot_overwrite_newer_fact(pipeline):
    store = pipeline.store
    use_llm(pipeline, summary=fact_summary("The Q4 budget cut is ten percent."))
    newer = record(pipeline, session="s2", start=T0 + 86400)
    new_id = store.get_conversation(newer)["facts"][0]["id"]
    use_llm(pipeline, summary=fact_summary("The Q4 budget cut is fifteen percent."),
            links=[{"new": 0, "relation": "updates", "existing": new_id}])
    record(pipeline)  # recorded earlier, summarized later
    assert [f["fact"] for f in store.list_facts()] == ["The Q4 budget cut is ten percent."]


def test_repeated_fact_becomes_one_fact_with_two_sources(pipeline):
    store = pipeline.store
    use_llm(pipeline, summary=fact_summary("Alice moves to Berlin in January."))
    first = record(pipeline)
    old_id = store.get_conversation(first)["facts"][0]["id"]
    use_llm(pipeline, summary=fact_summary("Alice is moving to Berlin in January."),
            links=[{"new": 0, "relation": "same", "existing": old_id},
                   {"new": 5, "relation": "same", "existing": old_id},  # bad index: ignored
                   {"new": 0, "relation": "same", "existing": 999}])  # unknown id: ignored
    second = record(pipeline, session="s2", start=T0 + 86400)
    assert store.get_conversation(second)["facts"] == []
    out = call(build_mcp(store, pipeline.settings), "list_facts")
    assert out.count("- [") == 1 and "said again 2025-10-10" in out and out.count("/view/") == 2
