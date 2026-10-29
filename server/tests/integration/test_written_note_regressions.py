"""QC report of 2026-10-26 on written notes (POST /v1/ingest/text, PR #108): one test
(or a few) per reported bug; see docs/archive/qc-report-2026-10-26.md for the verdicts.
Fictional data only."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient
from test_ingest_relay import MCP_TOKEN, mcp_client
from test_mcp import call
from test_written_notes import LINK, note_llm

from athena.api import create_app
from athena.domain import ERASED_TEXT
from athena.mcp_server import build_mcp
from athena.pipeline import MAX_LINE_CHARS, validate_link, written_lines
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer

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


# ------------------------------------------------------------------- A-1
def test_a1_deleting_a_note_keeps_what_its_forgotten_fact_replaced_replaced(pipeline):
    s = pipeline.store
    c1 = put(pipeline, "hub-note:one", ["Alice lives in Lisbon."])
    (f1,) = summarize(s, c1, [("Alice", "Alice lives in Lisbon", T0)])
    c2 = put(pipeline, "hub-note:two", ["Alice lives in Porto now."], T0 + DAY)
    (f2,) = summarize(s, c2, [("Alice", "Alice lives in Porto", T0 + DAY)])
    s.link_fact(f2, "updates", f1)
    assert s.erase_fact(f2, "private")["result"] == "erased"
    assert current(s) == set()

    s.delete_written_note("hub-note:two")
    assert current(s) == set()  # "Lisbon" stays an older value of a rejected belief
    assert s.list_facts(query="Lisbon") == []
    # ... also after its own note is summarized again
    summarize(s, c1, [("Alice", "Alice lives in Lisbon", T0)])
    assert current(s) == set()


def test_a1_deleting_a_note_still_brings_back_what_a_current_fact_replaced(pipeline):
    """Guard: a fact replaced by a current fact of the deleted note becomes current."""
    s = pipeline.store
    c1 = put(pipeline, "hub-note:one", ["Alice lives in Lisbon."])
    (f1,) = summarize(s, c1, [("Alice", "Alice lives in Lisbon", T0)])
    c2 = put(pipeline, "hub-note:two", ["Alice lives in Porto now."], T0 + DAY)
    (f2,) = summarize(s, c2, [("Alice", "Alice lives in Porto", T0 + DAY)])
    s.link_fact(f2, "updates", f1)
    s.delete_written_note("hub-note:two")
    assert current(s) == {"Alice lives in Lisbon"}


def test_a1_a_fact_erased_in_a_deleted_note_stays_erased_in_a_new_note(pipeline):
    s = pipeline.store
    c1 = put(pipeline, "hub-note:a", ["Secret passcode is Bluebananas42.", "Buy coffee."])
    (fid,) = summarize(s, c1, [("Me", "The secret passcode is Bluebananas42", T0)])
    s.erase_fact(fid, "private")
    s.delete_written_note("hub-note:a")

    c2 = put(pipeline, "hub-note:b", ["Secret passcode is Bluebananas42.", "Buy coffee."])
    assert texts(s, c2) == [ERASED_TEXT, "Buy coffee."]
    assert s.search_segments(query="Bluebananas42") == []


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


# ------------------------------------------------------------------- A-3
def test_a3_erase_after_a_resend_never_redacts_the_line_that_moved_up(pipeline):
    s = pipeline.store
    conv = put(pipeline, "hub-note:n1", ["Buy milk", "Secret: I hate my boss",
                                         "Call Mom on Sunday"])
    (fid,) = summarize(s, conv, [("Me", "The owner dislikes their manager", T0 + 1)],
                       [("Call Mom on Sunday", T0 + 2)])
    put(pipeline, "hub-note:n1", ["Buy milk", "Call Mom on Sunday"])  # the line crossed out
    s.erase_fact(fid, "private")
    assert texts(s, conv) == ["Buy milk", "Call Mom on Sunday"]
    assert json.loads(s.written_note("hub-note:n1")["erased_lines"]) == []


def test_a3_offsets_follow_their_line_on_resend(pipeline):
    s = pipeline.store
    conv = put(pipeline, "hub-note:n1", ["Buy milk", "Alice is vegetarian.",
                                         "Call Mom on Sunday"])
    summarize(s, conv, [("Alice", "Alice is vegetarian", T0 + 1)],
              [("Call Mom on Sunday", T0 + 2)])
    put(pipeline, "hub-note:n1", ["Weekend", "Buy milk", "Call Mom on Sunday",
                                  "Alice is a vegetarian now."])  # moved, and edited
    got = s.get_conversation(conv)
    assert [f["source_at"] for f in got["facts"]] == [T0 + 3]
    assert [a["source_at"] for a in got["action_items"]] == [T0 + 2]
    # and erasing it now redacts its line
    s.erase_fact(got["facts"][0]["id"])
    assert texts(s, conv) == ["Weekend", "Buy milk", "Call Mom on Sunday", ERASED_TEXT]


def test_a3_a_crossed_out_line_leaves_no_offset(pipeline):
    s = pipeline.store
    conv = put(pipeline, "hub-note:n1", ["Buy milk", "Alice is vegetarian."])
    summarize(s, conv, [("Alice", "Alice is vegetarian", T0 + 1)])
    put(pipeline, "hub-note:n1", ["Buy milk", "Call Mom"])
    assert [f["source_at"] for f in s.get_conversation(conv)["facts"]] == [None]


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


# ------------------------------------------------------------------- A-5
@pytest.mark.parametrize("link", [
    "https://hub.local/#open=notes&note=1)[evil](https://evil.example/x)",
    'https://hub.local/#open=notes&note=1"x',
    "https://hub.local/#open=notes&note=<1>",
    "https://hub.local/#open=notes&note=1'",
    "https://hub.local/a`b",
    "https://hub.local/#note=[1]",
    "http:///#open=notes",
    "http://user@evil.example@hub/",
])
def test_a5_links_that_break_the_label_are_refused(link):
    with pytest.raises(ValueError):
        validate_link(link)


@pytest.mark.parametrize("link", [
    LINK, "https://hub.example.ts.net/#open=notes&note=5f0c2a9e-1b7d",
    "http://[fd7a:115c:a1e0::1]:7000/#open=notes&note=x%20y",
])
def test_a5_hub_links_are_accepted(link):
    assert validate_link(link) == link


# ------------------------------------------------------------------- A-6
@pytest.fixture
def api(pipeline):
    pipeline.summarizer = Summarizer(
        LLMClient("http://llm.test/v1", "test-model", transport=note_llm([])), "Me")
    with TestClient(create_app(pipeline)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


@pytest.mark.parametrize("source_id", ["hub-note:.", "hub-note:..", "hub-note:..."])
def test_a6_dot_ids_can_be_deleted_over_http(api, pipeline, source_id):
    body = {"source_id": source_id, "text": "Buy milk.", "written_at": str(T0),
            "link": LINK}
    assert api.post("/v1/ingest/text", json=body).json()["result"] == "created"
    r = api.delete(f"/v1/ingest/text/{source_id}")
    assert r.status_code == 200 and r.json()["result"] == "deleted"
    relay = mcp_client(pipeline, httpx.ASGITransport(app=create_app(pipeline)))
    q = f"?access_token={MCP_TOKEN}"
    assert relay.post(f"/ingest/text{q}", json=body).json()["result"] == "created"
    r = relay.delete(f"/ingest/text/{source_id}{q}")
    assert r.status_code == 200 and r.json()["result"] == "deleted"


@pytest.mark.parametrize("source_id", [".", "..", "hub-note:../x", "hub-note:a/b"])
def test_a6_bare_dots_are_no_source_id(api, source_id):
    body = {"source_id": source_id, "text": "Buy milk.", "written_at": str(T0),
            "link": LINK}
    assert api.post("/v1/ingest/text", json=body).status_code == 400


# ------------------------------------------------------------------- A-7
def test_a7_a_failed_note_is_retried_by_the_worker_after_an_unchanged_resend(api, pipeline):
    from athena.pipeline import Worker

    conv = put(pipeline, "hub-note:n1", ["Call the dentist on Monday."])
    pipeline.store.mark_conversation_failed(conv, "LLMError: down")
    assert pipeline.ingest_text(source_id="hub-note:n1", text="Call the dentist on Monday.",
                                written_at=T0, link=LINK)["result"] == "unchanged"
    pipeline.settings.summary_retry_seconds = 0
    Worker(pipeline).tick()
    assert pipeline.store.get_conversation(conv)["status"] == "summarized"


# ------------------------------------------------------------------- A-8
def test_a8_digest_calls_a_written_note_a_written_note(pipeline):
    log: list = []
    pipeline.summarizer = Summarizer(
        LLMClient("http://llm.test/v1", "test-model", transport=note_llm(log)), "Me")
    conv = put(pipeline, "hub-note:n1", ["Call the dentist on Monday."])
    summarize(pipeline.store, conv)
    pipeline.summarizer.digest("2025-10-09", [pipeline.store.get_conversation(conv)])
    (sent,) = json.loads(log[-1]["messages"][-1]["content"])
    assert sent.get("written_note") is True and "voice_note" not in sent


def test_a8_list_facts_marks_repeats_and_history_from_written_notes(pipeline):
    s = pipeline.store
    c1 = put(pipeline, "hub-note:a", ["Alice lives in Lisbon."])
    (f1,) = summarize(s, c1, [("Alice", "Alice lives in Lisbon", T0)])
    c2 = pipeline.ingest_text(source_id="hub-note:b", text="Alice lives in Porto now.",
                              written_at=T0 + DAY, link=f"{LINK}-2")["conversation_id"]
    (f2,) = summarize(s, c2, [("Alice", "Alice lives in Porto", T0 + DAY)])
    s.link_fact(f2, "updates", f1)
    c3 = pipeline.ingest_text(source_id="hub-note:c", text="Alice lives in Porto.",
                              written_at=T0 + 2 * DAY, link=f"{LINK}-3")["conversation_id"]
    (f3,) = summarize(s, c3, [("Alice", "Alice lives in Porto", T0 + 2 * DAY)])
    s.link_fact(f3, "same", f2)
    text = call(build_mcp(s, pipeline.settings), "list_facts")
    said_again = next(ln for ln in text.splitlines() if "said again" in ln)
    replaces = next(ln for ln in text.splitlines() if "replaces" in ln)
    assert said_again.endswith(f"(written by hand: {LINK}-3)")
    assert replaces.endswith(f"(written by hand: {LINK})")


# ------------------------------------------------------------------- X-1
def test_x1_a_long_line_is_wrapped_at_word_boundaries():
    para = " ".join(["handwriting"] * 1000)  # 11,999 characters, no line break
    out = written_lines(para)
    assert len(out) > 1 and all(len(ln) <= MAX_LINE_CHARS for ln in out)
    assert " ".join(out) == para
    # one "word" longer than a line is cut
    out = written_lines("x" * (MAX_LINE_CHARS * 2 + 5))
    assert [len(ln) for ln in out] == [MAX_LINE_CHARS, MAX_LINE_CHARS, 5]


def test_x1_a_long_line_is_accepted_over_http(api, pipeline):
    body = {"source_id": "hub-note:long", "text": "word " * 1000, "written_at": str(T0),
            "link": LINK}
    r = api.post("/v1/ingest/text", json=body)
    assert r.status_code == 200 and r.json()["lines"] == 2


def test_x1_limits_that_stay(api, pipeline):
    """Guard: empty text is 400, over 200,000 characters 422, over 5,000 lines 400."""
    body = {"source_id": "hub-note:x", "written_at": str(T0), "link": LINK}
    assert api.post("/v1/ingest/text", json={**body, "text": " \n"}).status_code == 400
    assert api.post("/v1/ingest/text", json={**body, "text": "x" * 200_001}).status_code == 422
    assert api.post("/v1/ingest/text", json={**body, "text": "x\n" * 5001}).status_code == 400
    assert api.post("/v1/ingest/text", json={**body, "text": "x" * 200_000}).status_code == 200
