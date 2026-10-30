"""Which threads get a summary, when, in what order, and what the model is told
(texts/summary.py, Store.summary_queue, Pipeline.summarize_pending)."""

from __future__ import annotations

import json

from .helpers import T0, H, msg, thread_ids, upload

LONG = "Can you bring the blue folder with the lease papers to dinner tomorrow night?"


def test_recordings_first_then_quiet_threads_newest_first(pipeline):
    s = pipeline.settings
    pipeline.ingest_messages(upload(msg("a", T0, LONG), msg("b", T0 + 10 * H, LONG),
                                    msg("c", T0 + 20 * H, LONG)))
    old, mid, new = thread_ids(pipeline.store)
    pipeline.store._conn.execute(
        "INSERT INTO conversations (id, device, started_at, ended_at, updated_at)"
        " VALUES ('rec', 'omi', ?, ?, ?)", (T0 + 30 * H, T0 + 30 * H, T0))
    now = T0 + 20 * H + s.text_summary_quiet_seconds + 1
    queue = pipeline.store.summary_queue(now=now, quiet=s.text_summary_quiet_seconds,
                                         per_pass=2, archive_before=None)
    assert queue == ["rec", new, mid]
    # the newest thread isn't quiet yet a minute after its message
    queue = pipeline.store.summary_queue(now=T0 + 20 * H + 60,
                                         quiet=s.text_summary_quiet_seconds,
                                         per_pass=10, archive_before=None)
    assert queue == ["rec", mid, old]


def test_old_threads_are_stored_without_a_summary(pipeline):
    pipeline.ingest_messages(upload(msg("a", T0, LONG), msg("b", T0 + 400 * 24 * H, LONG)))
    old, new = thread_ids(pipeline.store)
    pipeline.summarize_pending(now=T0 + 400 * 24 * H + 3 * H)
    assert pipeline.store.get_conversation(old)["status"] == "discarded"
    assert pipeline.store.get_conversation(old)["discard_reason"] == "archived"
    assert pipeline.store.get_conversation(new)["status"] == "summarized"
    # summarizing an archived thread on request still works
    assert pipeline.summarize_conversation(old)
    assert pipeline.store.get_conversation(old)["status"] == "summarized"


def test_a_short_thread_gets_no_model_call(pipeline, llm_log):
    pipeline.ingest_messages(upload(msg("a", T0, "ok"), msg("b", T0 + 5, "👍", from_me=True)))
    [conv] = thread_ids(pipeline.store)
    pipeline.summarize_pending(now=T0 + 3 * H)
    c = pipeline.store.get_conversation(conv)
    assert (c["status"], c["discard_reason"]) == ("discarded", "short")
    assert llm_log == []
    # a new message makes it worth a summary again
    pipeline.ingest_messages(upload(msg("c", T0 + 60, LONG)))
    assert pipeline.store.get_conversation(conv)["status"] == "pending"


def test_the_model_is_told_these_are_texts_with_whom_and_when(pipeline, llm_log):
    pipeline.ingest_messages(upload(msg("a", T0, LONG), msg("b", T0 + 60, "Sure, will do",
                                                            from_me=True)))
    pipeline.summarize_pending(now=T0 + 3 * H)
    prompts = [json.dumps(c) for c in llm_log]
    assert any("Text messages with Mia Chen. These are text messages (imessage)" in p
               for p in prompts)
    assert any("Thursday 2025-10-09" in p for p in prompts)


def test_a_new_message_after_a_summary_summarizes_again_and_takes_down_the_digest(pipeline):
    pipeline.ingest_messages(upload(msg("a", T0, LONG)))
    [conv] = thread_ids(pipeline.store)
    pipeline.summarize_pending(now=T0 + 3 * H)
    assert pipeline.store.get_conversation(conv)["status"] == "summarized"
    pipeline.ingest_messages(upload(msg("b", T0 + 4 * H, "Also the keys please")))
    assert pipeline.store.get_conversation(conv)["status"] == "pending"
    assert thread_ids(pipeline.store) == [conv]
