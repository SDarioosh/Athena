"""Fewer, better facts for the hub's belief review.

A technical chat once gave five tentative beliefs, most of them noise: a request to
someone else stored as a "preference", thinking aloud ("is considering removing ..."),
which tool was used for a task, and a device left unnamed. Now:

* the facts prompt leaves those out, never guesses anyone's gender, and asks for a kind
  (preference, plan, decision, relationship, personal, other);
* code drops thinking aloud and requests to someone else;
* a second model call scores each fact 1-5 and drops those under ``fact_min_score``;
  at most ``max_facts`` are kept, best first;
* ``list_facts`` JSON rows carry ``kind``, ``conversation_id`` and ``quote`` (the line
  it was said on), so the hub labels facts by kind and shows the words without a click.

All names, devices and scenarios are fictional."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import fake_llm_transport
from test_confirmed_facts import T0
from test_mcp import call

from convo_server.llm import FACTS_SYSTEM, REVIEW_SYSTEM, LLMClient, Summarizer, fact_kind
from convo_server.mcp_server import build_mcp
from convo_server.store import Segment

LINES = ["Priya, can you write up how the Larkspur weather station joins the mesh?",
         "Sure, I'll send it to you next week.",
         "I'm allergic to shellfish, so not the seafood place.",
         "We decided to move the allotment to the Fernhill site in spring."]


def summarize(facts: list[dict], kind: str = "casual", scores: dict[str, int] | None = None,
              log: list | None = None, **opts) -> dict:
    summary = {"title": "Chat", "summary": "s", "key_points": [], "people": [],
               "topics": [], "action_items": [], "facts": facts}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(
        summary=summary, scores=scores, log=log))
    segments = [Segment(T0 + 10 * i, T0 + 10 * i + 5, line, "Me", True)
                for i, line in enumerate(LINES)]
    return Summarizer(llm, "Me", timezone="Europe/Berlin", **opts).summarize(
        segments, kind=kind)


def fact(text: str, kind: str = "other", at: int = 0, subject: str = "Me") -> dict:
    return {"subject": subject, "fact": text, "kind": kind, "at_seconds": at}


def texts(out: dict) -> list[str]:
    return [f["fact"] for f in out["facts"]]


def test_prompt_leaves_out_noise_and_asks_for_a_kind():
    for words in ("thinking aloud", "request to someone else", "which tool was",
                  'never "he"', "kind: preference", "relationship", "personal",
                  "Fewer good facts"):
        assert words in FACTS_SYSTEM
    for words in ("three months", "thinking aloud", "which tool was used", '"the device"'):
        assert words in REVIEW_SYSTEM


@pytest.mark.parametrize("kind", ["casual", "note"])
def test_thinking_aloud_and_requests_are_dropped(kind):
    out = summarize([
        fact("Me is considering removing the GPS module from the Larkspur weather station."),
        fact("Me might repaint the garden shed."),
        fact("Me is thinking about learning the cello."),
        fact("Me wants Priya Okafor to write a report on the mesh setup and send it back."),
        fact("Me asked him to bring the spare antenna."),
        fact("Me is allergic to shellfish.", "personal", 20),
    ], kind=kind)
    assert texts(out) == ["Me is allergic to shellfish."]


def test_decisions_wishes_and_possessions_stay():
    out = summarize([
        fact("Me decided to move the allotment to the Fernhill site in spring.", "decision"),
        fact("Me wants to visit Lake Bled next summer.", "plan"),
        fact("Me needs her passport to travel to Ghana in May.", "plan"),
    ], max_facts=0)
    assert len(out["facts"]) == 3


def test_review_drops_low_scores_and_the_cap_keeps_the_best_in_order():
    facts = [fact("Me uses a soldering iron to fix the weather station."),
             fact("Me is allergic to shellfish.", "personal", 20),
             fact("Me plans to post the weather station code publicly."),
             fact("Me decided to move the allotment to the Fernhill site.", "decision", 30),
             fact("Me prefers tea to coffee.", "preference"),
             fact("Me's sister Ines lives in Porto.", "relationship")]
    scores = {facts[0]["fact"]: 1, facts[2]["fact"]: 2, facts[4]["fact"]: 4,
              facts[5]["fact"]: 5}
    out = summarize(facts, scores=scores, max_facts=3)
    # 1 and 2 under the minimum; of the four left, the 4 (tea) is over the cap
    assert texts(out) == ["Me is allergic to shellfish.",
                          "Me decided to move the allotment to the Fernhill site.",
                          "Me's sister Ines lives in Porto."]


def test_review_sees_the_line_each_fact_came_from():
    log: list = []
    summarize([fact("Me is allergic to shellfish.", "personal", 20)], log=log)
    (review,) = [b for b in log if b["response_format"]["json_schema"]["name"] == "FactReview"]
    asked = json.loads(review["messages"][-1]["content"])["facts"]
    assert asked == [{"number": 0, "subject": "Me", "fact": "Me is allergic to shellfish.",
                      "said": LINES[2]}]


def test_no_review_call_at_score_one_and_no_cap_at_zero():
    log: list = []
    facts = [fact(f"Me owns {n} bicycles.") for n in range(2, 8)]
    out = summarize(facts, log=log, fact_min_score=1, max_facts=0)
    assert len(out["facts"]) == 6
    assert "FactReview" not in [b["response_format"]["json_schema"]["name"] for b in log]


def test_a_failed_review_keeps_the_facts_but_still_caps():
    summary = {"title": "Chat", "summary": "s", "key_points": [], "people": [],
               "topics": [], "action_items": [],
               "facts": [fact(f"Me owns {n} bicycles.") for n in range(2, 7)]}
    inner = fake_llm_transport(summary=summary)

    def handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        if name == "FactReview":
            return httpx.Response(503, json={"error": "down"})
        return inner.handler(request)

    llm = LLMClient("http://llm.test/v1", "m", transport=httpx.MockTransport(handler))
    out = Summarizer(llm, "Me", max_facts=2).summarize(
        [Segment(T0, T0 + 5, "We talked about bicycles for a while.", "Me", True)])
    assert texts(out) == ["Me owns 2 bicycles.", "Me owns 3 bicycles."]


def test_kinds_are_kept_and_odd_ones_become_other():
    out = summarize([fact("Me prefers tea to coffee.", "Preference"),
                     fact("Me's sister Ines lives in Porto.", "family ties")], max_facts=0)
    assert [f["kind"] for f in out["facts"]] == ["preference", "other"]
    assert fact_kind(None) == "other" and fact_kind("plan") == "plan"


def test_voice_note_decisions_get_a_kind_and_skip_the_cap():
    items = [{"text": "Stop prioritizing side projects before the base system is stable.",
              "owner": "Me", "at_seconds": 0}]
    summary = {"title": "Plans", "summary": "s", "key_points": [], "people": [],
               "topics": [], "action_items": items,
               "facts": [fact(f"Me owns {n} bicycles.") for n in range(2, 6)]}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    out = Summarizer(llm, "Me", max_facts=1).summarize(
        [Segment(T0, T0 + 5, "Stop prioritizing side projects before the base system is "
                 "stable.", "Me", True)], kind="note")
    kinds = {f["fact"]: f["kind"] for f in out["facts"]}
    assert kinds == {"Me owns 2 bicycles.": "other",
                     "The owner decided to stop prioritizing side projects before the base "
                     "system is stable.": "decision"}


def conversation(store, name: str, line: str, facts: list[dict]) -> str:
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=T0, duration=30,
                    codec="wav", audio_path=None, speech=[])
    store.replace_segments(name, "omi", [Segment(T0, T0 + 5, line, "Me", True)],
                           [(T0, T0 + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments(name)])
    store.save_summary(conv, {"title": name, "summary": "s", "key_points": [],
                              "topics": [], "people": [], "action_items": [],
                              "facts": facts})
    return conv


def json_facts(pipeline) -> dict[str, dict]:
    out = json.loads(call(build_mcp(pipeline.store, pipeline.settings), "list_facts",
                          format="json"))
    return {f["fact"]: f for f in out["facts"]}


def test_json_rows_carry_kind_conversation_and_quote(pipeline):
    store = pipeline.store
    conv = conversation(store, "c1", "Honestly I'm allergic to shellfish, always have been.",
                        [{"subject": "Me", "fact": "Me is allergic to shellfish.",
                          "kind": "personal", "source_at": T0 + 1},
                         {"subject": "Me", "fact": "Me prefers tea to coffee.",
                          "kind": "preference", "source_at": None}])
    rows = json_facts(pipeline)
    shellfish = rows["Me is allergic to shellfish."]
    assert shellfish["kind"] == "personal" and shellfish["conversation_id"] == conv
    assert shellfish["quote"] == "Honestly I'm allergic to shellfish, always have been."
    assert rows["Me prefers tea to coffee."]["quote"] is None  # no line to quote


def test_an_erased_line_is_never_quoted(pipeline):
    store = pipeline.store
    conversation(store, "c1", "My sister Ines moved to Porto last year.",
                 [{"subject": "Me", "fact": "Me's sister Ines lives in Porto.",
                   "kind": "relationship", "source_at": T0 + 1},
                  {"subject": "Me", "fact": "Me prefers tea to coffee.",
                   "kind": "preference", "source_at": T0 + 1}])
    rows = json_facts(pipeline)
    store.erase_fact(rows["Me's sister Ines lives in Porto."]["id"])
    rows = json_facts(pipeline)
    assert rows["Me prefers tea to coffee."]["quote"] is None


def test_a_re_summary_keeps_the_kind(pipeline):
    store = pipeline.store
    facts = [{"subject": "Me", "fact": "Me prefers tea to coffee.", "kind": "preference",
              "source_at": T0 + 1}]
    conv = conversation(store, "c1", "Tea for me, never coffee.", facts)
    store.save_summary(conv, {"title": "again", "summary": "s", "key_points": [],
                              "topics": [], "people": [], "action_items": [],
                              "facts": facts})
    assert json_facts(pipeline)["Me prefers tea to coffee."]["kind"] == "preference"
