"""QC report of 2026-10-26 on written notes (POST /v1/ingest/text, PR #108): one test
(or a few) per reported bug; see docs/qc-report-2026-10-26.md for the verdicts.
Fictional data only."""

from __future__ import annotations

import json

from test_written_notes import LINK

from convo_server.store import ERASED_TEXT

T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC
DAY = 86_400.0


def put(pipeline, source_id: str, lines: list[str], at: float = T0) -> str:
    return pipeline.ingest_text(source_id=source_id, text="\n".join(lines), written_at=at,
                                link=LINK)["conversation_id"]


def summarize(store, conv_id: str, facts=(), items=()) -> list[int]:
    """Save a summary with ``facts`` [(subject, fact, source_at)] and ``items``
    [(text, source_at)], as the worker would; returns the new facts' ids."""
    store.save_summary(conv_id, {
        "title": "A note", "summary": "A written note.", "key_points": [], "topics": [],
        "people": [], "kind": "note",
        "action_items": [{"text": t, "owner": "Me", "source_at": at} for t, at in items],
        "facts": [{"subject": s, "fact": f, "source_at": at} for s, f, at in facts]})
    return [f["id"] for f in store.get_conversation(conv_id)["facts"]]


def texts(store, conv_id: str) -> list[str]:
    return [s.text for s in store.conversation_segments(conv_id)]


def current(store) -> set[str]:
    return {f["fact"] for f in store.list_facts()}


# ------------------------------------------------------------------- A-2
def test_a2_a_resend_redacts_only_lines_that_say_the_erased_fact(pipeline):
    s = pipeline.store
    conv = put(pipeline, "hub-note:n1", ["Alice lives in Berlin.", "Buy coffee tomorrow."])
    (fid,) = summarize(s, conv, [("Alice", "Alice lives in Berlin", T0)])
    s.erase_fact(fid, "private")
    keys = json.loads(s.written_note("hub-note:n1")["erased_lines"])
    assert len(keys) == 1

    innocent = ["Alice called from the Berlin office to discuss the Q4 budget.",
                "Alice is living with her sister now.", "Buy coffee tomorrow."]
    out = pipeline.ingest_text(source_id="hub-note:n1", text="\n".join(innocent),
                               written_at=T0, link=LINK)
    assert out["erased_lines"] == 0
    assert texts(s, conv) == innocent
    assert json.loads(s.written_note("hub-note:n1")["erased_lines"]) == keys


def test_a2_a_resend_still_redacts_the_fact_in_other_words(pipeline):
    """Guard: the erased words, as they were or with all of the fact's words, stay
    forgotten, and a fuzzy match is not fingerprinted (the next resend checks again)."""
    s = pipeline.store
    conv = put(pipeline, "hub-note:n1", ["Alice lives in Berlin.", "Buy coffee tomorrow."])
    (fid,) = summarize(s, conv, [("Alice", "Alice lives in Berlin", T0)])
    s.erase_fact(fid, "private")
    keys = json.loads(s.written_note("hub-note:n1")["erased_lines"])
    out = pipeline.ingest_text(
        source_id="hub-note:n1", written_at=T0, link=LINK,
        text="Alice lives in Berlin.\nIn Berlin: Alice, who lives there now.\nBuy coffee.")
    assert out["erased_lines"] == 2
    assert texts(s, conv) == [ERASED_TEXT, ERASED_TEXT, "Buy coffee."]
    assert json.loads(s.written_note("hub-note:n1")["erased_lines"]) == keys


# ------------------------------------------------------------------- A-4
def test_a4_a_fact_erased_in_one_note_is_redacted_in_another(pipeline):
    s = pipeline.store
    c1 = put(pipeline, "hub-note:a", ["Secret passcode is Bluebananas42."])
    (fid,) = summarize(s, c1, [("Me", "The secret passcode is Bluebananas42", T0)])
    s.erase_fact(fid)
    c2 = put(pipeline, "hub-note:b", ["Secret passcode is Bluebananas42.", "Buy coffee."],
             T0 + DAY)
    assert texts(s, c2) == [ERASED_TEXT, "Buy coffee."]
    assert s.search_segments(query="Bluebananas42") == []


def test_a4_only_erased_facts_are_redacted_in_other_notes(pipeline):
    """Guard: a retired (not erased) fact's words may be written again."""
    s = pipeline.store
    c1 = put(pipeline, "hub-note:a", ["Alice lives in Berlin."])
    (fid,) = summarize(s, c1, [("Alice", "Alice lives in Berlin", T0)])
    s.retire_fact(fid, "wrong")
    c2 = put(pipeline, "hub-note:b", ["Alice lives in Berlin."], T0 + DAY)
    assert texts(s, c2) == ["Alice lives in Berlin."]


