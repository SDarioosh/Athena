"""erase_fact: the owner forgets a fact in the hub and its words disappear where they
were said (transcript line, summary, key points, digest, search, embeddings, kept audio),
while the rest of the conversation stays. Fictional data."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
from test_confirmed_facts import DAY, HOUR, T0
from test_mcp import call

from athena.domain import ERASED_TEXT, Segment
from athena.mcp_server import build_mcp
from athena.summarize.client import LLMClient, LLMError
from athena.summarize.rules.facts import scrub_facts
from athena.summarize.summarizer import Summarizer

FACT = "Alice is vegetarian"


def rewriter(pipeline, notes: dict | None = None, fail: bool = False, log: list | None = None):
    """Give the pipeline a fake model that answers the forget rewrite with ``notes``."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if log is not None:
            log.append(body)
        if fail:
            return httpx.Response(503, json={"error": "down"})
        content = notes or {"title": "Lunch plans", "summary": "They planned lunch.",
                            "key_points": ["Lunch on Friday"], "topics": ["lunch"]}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(content)}}]})

    pipeline.summarizer = Summarizer(
        LLMClient("http://llm.test/v1", "test-model", transport=httpx.MockTransport(handler)),
        "Me")


def conversation(store, name: str, start: float, fact: str = FACT,
                 audio_path: str | None = None) -> tuple[str, int]:
    """Three lines; the middle one (start + 10) states ``fact``, the summary repeats it."""
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                    duration=30, codec="wav", audio_path=audio_path, speech=[])
    store.replace_segments(name, "omi", [
        Segment(start, start + 4, "Shall we get lunch on Friday", "Me", True),
        Segment(start + 10, start + 14, f"Remember {fact.lower()}, zucchini-zebra", "Bob"),
        Segment(start + 20, start + 24, "The taqueria then", "Me", True),
    ], [(start, start + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments(name)])
    (fid,) = store.save_summary(conv, {
        "title": "Lunch plans", "summary": f"They planned lunch. {fact}.",
        "key_points": ["Lunch on Friday", fact], "topics": ["lunch"], "people": ["Alice"],
        "action_items": [],
        "facts": [{"subject": "Alice", "fact": fact, "source_at": start + 10}]})
    return conv, fid


def erase(pipeline, fact_id: int) -> str:
    return call(build_mcp(pipeline.store, pipeline.settings), "erase_fact", fact_id=fact_id,
                reason="private")


def texts(store, conv: str) -> list[str]:
    return [s.text for s in store.conversation_segments(conv)]


def test_erase_redacts_the_line_and_hides_the_notes_until_rewritten(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    store.put_embedding("conversation", conv, conv, "m", np.ones(4))
    store.put_embedding("fact", str(fid), conv, "m", np.ones(4))
    out = erase(pipeline, fid)
    assert out == (f"Fact #{fid} erased: 1 transcript line(s) redacted, 1 conversation "
                   "summary(ies) being rewritten.")
    # the line that said it reads [forgotten]; the rest of the conversation stays
    assert texts(store, conv) == ["Shall we get lunch on Friday", ERASED_TEXT,
                                  "The taqueria then"]
    c = store.get_conversation(conv)
    assert c["forget_pending"] and c["summary"] is None and c["key_points"] == []
    assert c["facts"] == []
    # out of every search: lines, transcript and summary index, embeddings
    assert store.search_segments(query="zebra") == []
    assert store.fts_search("zebra") == [] and store.fts_search("vegetarian") == []
    assert store.search_segments(query="taqueria")
    assert store.all_embeddings("m") == []
    mcp = build_mcp(store, pipeline.settings)
    assert "being rewritten" in call(mcp, "get_conversation", conversation_id=conv)
    transcript = call(mcp, "get_transcript", conversation_id=conv)
    assert "zebra" not in transcript and ERASED_TEXT in transcript
    assert "vegetarian" not in call(mcp, "list_facts")

    rewriter(pipeline)
    assert pipeline.rewrite_forgotten() == 1
    c = store.get_conversation(conv)
    assert not c["forget_pending"]
    assert (c["title"], c["summary"], c["key_points"]) == (
        "Lunch plans", "They planned lunch.", ["Lunch on Friday"])
    assert [h["kind"] for h in store.fts_search("planned")] == ["conversation"]
    assert store.pending_rewrites() == []


def test_the_erased_words_leave_the_database_file(pipeline):
    store = pipeline.store
    _, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline)
    pipeline.rewrite_forgotten()
    db = Path(store.path)
    raw = b"".join(p.read_bytes() for p in db.parent.glob(db.name + "*") if p.is_file())
    assert b"zucchini" not in raw and b"zebra" not in raw
    assert b"taqueria" in raw  # the rest is still there


def test_the_model_cant_keep_the_exact_sentence(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline, {"title": "Alice is vegetarian", "summary":
                        "They planned lunch. Alice is vegetarian! Bob pays.",
                        "key_points": ["alice is VEGETARIAN", "Friday"], "topics": ["lunch"]})
    pipeline.rewrite_forgotten()
    c = store.get_conversation(conv)
    assert (c["title"], c["summary"], c["key_points"]) == (
        "Conversation", "They planned lunch. Bob pays.", ["Friday"])


def test_scrub_matches_whole_words_only():
    out = scrub_facts({"title": "t", "summary": "Bob is tall. Bobby is tall.",
                       "key_points": [], "topics": []}, ["Bob is tall"])
    assert out["summary"] == "Bobby is tall."


def test_erase_is_idempotent_and_reports_unknown_ids(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    assert erase(pipeline, fid) == f"Fact #{fid} was already erased."
    assert len(store.pending_rewrites()) == 1
    assert erase(pipeline, 9999) == "No fact #9999."
    assert texts(store, conv)[1] == ERASED_TEXT


def test_a_fact_retired_earlier_can_be_erased(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    call(build_mcp(store, pipeline.settings), "retire_fact", fact_id=fid)
    assert "erased: 1 transcript line(s)" in erase(pipeline, fid)
    assert texts(store, conv)[1] == ERASED_TEXT


def test_every_conversation_that_said_it_is_erased(pipeline):
    store = pipeline.store
    first, fid = conversation(store, "c0", T0)
    second, again = conversation(store, "c1", T0 + DAY)
    store.link_fact(again, "same", fid)
    assert "2 transcript line(s)" in erase(pipeline, fid)
    assert texts(store, first)[1] == texts(store, second)[1] == ERASED_TEXT
    assert {r["conversation_id"] for r in store.pending_rewrites()} == {first, second}


def test_a_repeat_said_after_the_forget_is_erased_too(pipeline):
    store = pipeline.store
    _, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline)
    pipeline.rewrite_forgotten()
    later, again = conversation(store, "c1", T0 + DAY)
    store.link_fact(again, "same", fid)
    assert texts(store, later)[1] == ERASED_TEXT
    assert store.get_conversation(later)["forget_pending"]
    assert store.facts_by_id([again]) == []
    assert "vegetarian" not in call(build_mcp(store, pipeline.settings), "list_facts")


def test_a_resummary_keeps_it_erased_and_rewrites_again(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline)
    pipeline.rewrite_forgotten()
    # a speaker rename re-summarizes; this model still says the fact (from elsewhere)
    store.save_summary(conv, {
        "title": "Lunch", "summary": f"Lunch. {FACT}.", "key_points": [], "topics": [],
        "people": [], "action_items": [],
        "facts": [{"subject": "Alice", "fact": FACT, "source_at": T0 + 10}]})
    row = store._one("SELECT * FROM facts WHERE conversation_id = ?", (conv,))
    assert row["status"] == "retired" and row["erased_at"] is not None
    c = store.get_conversation(conv)
    assert c["forget_pending"] and c["summary"] is None
    assert store.pending_rewrites()[0]["summary"] == f"Lunch. {FACT}."
    assert "already erased" in erase(pipeline, row["id"])


def test_kept_audio_of_the_line_is_deleted(pipeline, tmp_path):
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"RIFF")
    _, fid = conversation(pipeline.store, "c0", T0, audio_path=str(audio))
    erase(pipeline, fid)
    assert not audio.exists()
    assert pipeline.store.chunks("c0")[0]["audio_path"] is None


def test_a_digest_is_taken_down_and_written_again_after_the_rewrite(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    store.put_digest("2025-10-09", {"headline": FACT, "summary": FACT, "highlights": [],
                                    "open_action_items": [], "people": [],
                                    "conversation_ids": [conv]})
    erase(pipeline, fid)
    assert store.get_digest("2025-10-09") is None
    assert "No digest" in call(build_mcp(store, pipeline.settings), "get_daily_digest",
                               date="2025-10-09")
    log: list = []
    rewriter(pipeline, log=log)
    # the fake answers every call with the rewrite; the digest call gets its own schema
    pipeline.summarizer.llm.structured = _digest_aware(pipeline.summarizer.llm.structured)
    pipeline.rewrite_forgotten()
    digest = store.get_digest("2025-10-09")
    assert digest is not None and "vegetarian" not in json.dumps(digest).lower()
    assert store.pending_digests() == []


def _digest_aware(structured):
    from athena.summarize.schemas import DailyDigest

    def wrapped(system, user, schema, retries=1):
        if schema is DailyDigest:
            assert "vegetarian" not in user.lower()
            return DailyDigest(headline="Lunch", summary="Lunch plans.", highlights=[],
                               open_action_items=[], people=[])
        return structured(system, user, schema, retries)
    return wrapped


def test_a_model_failure_keeps_the_notes_hidden(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline, fail=True)
    assert pipeline.rewrite_forgotten() == 0
    assert store.get_conversation(conv)["forget_pending"]
    assert store.pending_rewrites()[0]["attempts"] == 1
    rewriter(pipeline)
    assert pipeline.rewrite_forgotten() == 1


def test_the_view_page_shows_the_redacted_line(pipeline):
    from fastapi.testclient import TestClient

    from athena.api import create_app
    from athena.links import sign

    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    app = create_app(pipeline)
    page = TestClient(app).get(f"/view/{conv}?sig={sign(pipeline.settings, conv)}").text
    assert "zebra" not in page and ERASED_TEXT in page and "taqueria" in page
    assert "rewritten" in page


def test_other_facts_of_the_conversation_stay(pipeline):
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0 + HOUR)
    other = store._conn.execute(
        "INSERT INTO facts (conversation_id, subject, fact, source_at) VALUES (?, ?, ?, ?)",
        (conv, "Me", "Me likes tacos", T0 + HOUR + 20)).lastrowid
    erase(pipeline, fid)
    assert [f["id"] for f in store.get_conversation(conv)["facts"]] == [other]
    assert texts(store, conv)[2] == "The taqueria then"


def test_two_facts_of_one_line_never_redact_the_line_before(pipeline, tmp_path):
    """QC S1: a line that ends less than a second before the erased line was taken as
    the line of a second fact said on the erased line, and its kept audio deleted."""
    store = pipeline.store
    before, line = tmp_path / "c0.wav", tmp_path / "c1.wav"
    before.write_bytes(b"RIFF")
    line.write_bytes(b"RIFF")
    store.add_chunk(session_id="s", device="omi", sequence=0, started_at=T0, duration=19.5,
                    codec="wav", audio_path=str(before), speech=[])
    store.add_chunk(session_id="s", device="omi", sequence=1, started_at=T0 + 19.5,
                    duration=10, codec="wav", audio_path=str(line), speech=[])
    store.replace_segments("s", "omi", [
        Segment(T0, T0 + 19.5, "We should book the taqueria for Friday", "Me", True),
        Segment(T0 + 20, T0 + 25, "Alice is vegetarian and Bob is vegan, zebra", "Bob"),
    ], [(T0, T0 + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments("s")])
    first, second = store.save_summary(conv, {
        "title": "Lunch", "summary": "Lunch.", "key_points": [], "topics": [],
        "people": [], "action_items": [],
        "facts": [{"subject": "Alice", "fact": FACT, "source_at": T0 + 20},
                  {"subject": "Bob", "fact": "Bob is vegan", "source_at": T0 + 20}]})
    assert store.erase_fact(first)["lines"] == 1
    assert store.erase_fact(second)["lines"] == 0  # its line is already redacted
    assert texts(store, conv) == ["We should book the taqueria for Friday", ERASED_TEXT]
    assert before.exists() and not line.exists()


def test_a_resummary_after_the_fact_moved_to_another_source_waits_for_the_rewrite(pipeline):
    """QC S2: an erased fact said in two conversations is owned by the other one after
    its own conversation's re-summary drops it; that re-summary still waits for the
    forget rewrite instead of showing its text."""
    store = pipeline.store
    first, fid = conversation(store, "c0", T0)
    second, again = conversation(store, "c1", T0 + DAY)
    store.link_fact(again, "same", fid)
    erase(pipeline, fid)
    rewriter(pipeline)
    assert pipeline.rewrite_forgotten() == 2
    # a speaker rename re-summarizes the first conversation; the fact isn't said again
    store.save_summary(first, {
        "title": "Lunch", "summary": f"Lunch. {FACT}.", "key_points": [], "topics": [],
        "people": [], "action_items": [], "facts": []})
    assert store._one("SELECT conversation_id FROM facts WHERE id = ?",
                      (fid,))["conversation_id"] == second
    c = store.get_conversation(first)
    assert c["forget_pending"] and c["summary"] is None
    assert store.fts_search("vegetarian") == []


def test_a_digest_waits_for_every_conversation_of_its_day(pipeline):
    """QC S3: a digest taken down by a forget was dropped for good when another
    conversation of that day (not in the old digest) still waited for its rewrite."""
    store = pipeline.store
    conv, fid = conversation(store, "c0", T0)
    store.put_digest("2025-10-09", {"headline": FACT, "summary": FACT, "highlights": [],
                                    "open_action_items": [], "people": [],
                                    "conversation_ids": [conv]})
    erase(pipeline, fid)
    # a conversation of the same day, summarized after that digest was written
    later, other = conversation(store, "c1", T0 + HOUR, fact="Bob is vegan")
    erase(pipeline, other)
    rewriter(pipeline)
    structured = _digest_aware(pipeline.summarizer.llm.structured)
    calls = {"n": 0}

    def flaky(system, user, schema, retries=1):
        calls["n"] += 1
        if calls["n"] == 2:  # the second conversation's rewrite fails
            raise LLMError("llama-server HTTP 503")
        return structured(system, user, schema, retries)

    pipeline.summarizer.llm.structured = flaky
    assert pipeline.rewrite_forgotten() == 1
    assert store.get_digest("2025-10-09") is None
    assert [d["date"] for d in store.pending_digests()] == ["2025-10-09"]
    pipeline.rewrite_forgotten()  # llama-server is back
    assert store.get_conversation(later)["forget_pending"] is False
    assert store.get_digest("2025-10-09") is not None
    assert store.pending_digests() == []


def test_a_merged_conversation_stays_erased(pipeline):
    """QC S2 (merge): when a conversation an erased fact was said in is merged into
    another, the merged one counts as erased: a second erase changes nothing and its
    re-summary waits for the rewrite."""
    store = pipeline.store
    first, fid = conversation(store, "c0", T0)
    erase(pipeline, fid)
    rewriter(pipeline)
    pipeline.rewrite_forgotten()
    store.add_chunk(session_id="c1", device="omi", sequence=0, started_at=T0 + 60,
                    duration=10, codec="wav", audio_path=None, speech=[])
    store.replace_segments("c1", "omi", [Segment(T0 + 60, T0 + 64, "See you then", "Me",
                                                 True)], [(T0 + 60, T0 + 70)])
    merged = store.save_conversation(None, "omi",
                                     [s.id for s in store.session_segments("c1")],
                                     absorb=[first])
    assert erase(pipeline, fid) == f"Fact #{fid} was already erased."
    assert store.pending_rewrites() == []
    store.save_summary(merged, {
        "title": "Lunch", "summary": f"Lunch. {FACT}.", "key_points": [], "topics": [],
        "people": [], "action_items": [], "facts": []})
    assert store.get_conversation(merged)["forget_pending"]
