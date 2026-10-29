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

from athena.domain import Segment
from athena.mcp_server import build_mcp
from athena.summarize.client import LLMClient
from athena.summarize.prompts import FACTS_SYSTEM, REVIEW_SYSTEM
from athena.summarize.rules.facts import fact_kind
from athena.summarize.summarizer import Summarizer
from tests.conftest import fake_llm_transport, make_summarizer
from tests.unit.mcp.test_mcp import call
from tests.unit.store.test_confirmed_facts import T0

LINES = ["Priya, can you write up how the Larkspur weather station joins the mesh?",
         "Sure, I'll send it to you next week.",
         "I'm allergic to shellfish, so not the seafood place.",
         "We decided to move the allotment to the Fernhill site in spring."]


def summarize(facts: list[dict], kind: str = "casual", scores: dict[str, int] | None = None,
              log: list | None = None, people: list[str] | None = None, known=None,
              items: list[dict] | None = None, **opts) -> dict:
    summary = {"title": "Chat", "summary": "s", "key_points": [], "people": people or [],
               "topics": [], "action_items": items or [], "facts": facts}
    segments = [Segment(T0 + 10 * i, T0 + 10 * i + 5, line, "Me", True)
                for i, line in enumerate(LINES)]
    return make_summarizer(summary, scores=scores, log=log, **opts).summarize(
        segments, kind=kind, known=known)


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
    ], kind=kind, people=["Priya Okafor"])
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


# --- QC report fixes (fictional data) ------------------------------------------------

def kept(texts_in: list[str], people: list[str] | None = None) -> list[str]:
    return texts(summarize([fact(t) for t in texts_in], people=people, max_facts=0))


def test_undecided_is_thinking_aloud():
    assert kept(["Me hasn't decided whether to take the Zurich role.",
                 "Me might move to Porto, but hasn't decided yet.",
                 "Me has not decided whether to sell the boat.",
                 "Me is currently considering moving to Porto.",
                 "Me is seriously thinking about selling the kayak.",
                 "Me decided to keep the flat in Lisbon."]) == [
        "Me decided to keep the flat in Lisbon."]


def test_a_hedge_outside_the_main_clause_is_no_hedge():
    facts = ["Me has a severe peanut allergy that might cause anaphylaxis.",
             "Me plans to move to Lisbon in June, though the start date might shift.",
             "Priya is debating in the regional tournament."]
    assert kept(facts) == facts


def test_requests_are_told_apart_from_needs_and_plans():
    people = ["Bo Lindqvist", "Priya", "Leo", "Jan van Dijk"]
    facts = ["Me needs German to work at the Zurich office.",
             "Me needs Tailscale to reach the home server.",
             "Me wants Friday to stay free for deep work.",
             "Me wants Leo to attend the bilingual school in autumn.",
             "Me wants Dr. Patel to send the lab results.",
             "Me asked Ana Maria Silva to call the clinic.",
             "Me asked Bo and Priya to test the relay.",
             "Me asked the landlord to fix the boiler.",
             "Priya asked me to send the slides.",
             "Me asked Bo not to touch the router.",
             "Me wants Bo Lindqvist to write a report on the mesh setup.",
             "Me wants Jan van Dijk to SSH into the server."]
    assert kept(facts, people) == facts[:4]


def test_the_review_does_not_reject_facts_about_things():
    assert "not about anyone" not in REVIEW_SYSTEM
    assert "named project, place or thing is fine" in REVIEW_SYSTEM


def test_the_prompt_asks_for_more_candidates_than_the_cap():
    log: list = []
    summarize([fact("Me is allergic to shellfish.")], log=log, max_facts=3)
    (facts_call,) = [b for b in log
                     if b["response_format"]["json_schema"]["name"] == "FactList"]
    assert "List at most 6 facts" in facts_call["messages"][0]["content"]


def test_voice_note_decisions_from_the_fact_list_skip_review_and_cap():
    facts = [fact(f"Me owns {n} bicycles.") for n in range(2, 5)]
    facts.append(fact("Me decided to keep the flat in Lisbon.", "decision", 30))
    scores = {f["fact"]: 5 for f in facts[:3]} | {facts[3]["fact"]: 2}
    out = summarize(facts, kind="note", scores=scores, max_facts=2)
    assert "Me decided to keep the flat in Lisbon." in texts(out)
    assert len(out["facts"]) == 3  # two capped bicycles + the decision


def test_voice_note_event_facts_do_not_take_cap_places():
    items = [{"text": "Meeting with Priya", "kind": "event", "due": "Tuesday",
              "due_date": "2025-10-14", "time": "10am", "owner": "Me", "at_seconds": 0}]
    facts = [fact("Me has a meeting with Priya on Tuesday at 10am."),
             fact("Me is allergic to shellfish.", "personal", 20),
             fact("Me's sister Ines lives in Porto.", "relationship", 30)]
    out = summarize(facts, kind="note", items=items, max_facts=2)
    assert texts(out) == ["Me is allergic to shellfish.", "Me's sister Ines lives in Porto."]


def test_the_review_sees_every_line_of_a_shared_second():
    log: list = []
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary={
        "title": "t", "summary": "s", "key_points": [], "people": [], "topics": [],
        "action_items": [], "facts": [fact("Me is allergic to shellfish.", at=10)]},
        log=log))
    Summarizer(llm, "Me").summarize([
        Segment(T0, T0 + 5, "So where shall we eat tonight then?", "Priya", False),
        Segment(T0 + 10.1, T0 + 10.5, "Yeah.", "Priya", False),
        Segment(T0 + 10.7, T0 + 13, "I'm allergic to shellfish.", "Me", True)])
    (review,) = [b for b in log if b["response_format"]["json_schema"]["name"] == "FactReview"]
    said = json.loads(review["messages"][-1]["content"])["facts"][0]["said"]
    assert "allergic to shellfish" in said


def test_unscored_facts_count_as_the_minimum_score():
    facts = [fact("Me owns 2 bicycles."), fact("Me owns 3 bicycles."),
             fact("Me owns 4 bicycles.")]
    inner = fake_llm_transport(summary={"title": "t", "summary": "s", "key_points": [],
                                        "people": [], "topics": [], "action_items": [],
                                        "facts": facts})

    def handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        if name == "FactReview":  # no score for number 0
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
                {"scores": [{"number": 1, "score": 5}, {"number": 2, "score": 5}]})}}]})
        return inner.handler(request)

    llm = LLMClient("http://llm.test/v1", "m", transport=httpx.MockTransport(handler))
    segments = [Segment(T0, T0 + 5, "We talked about bicycles for a while.", "Me", True)]
    assert texts(Summarizer(llm, "Me", max_facts=3).summarize(segments)) == \
        [f["fact"] for f in facts]  # kept, at the minimum score
    assert texts(Summarizer(llm, "Me", max_facts=2).summarize(segments)) == \
        ["Me owns 3 bicycles.", "Me owns 4 bicycles."]  # ranked under the scored ones


def test_a_fact_that_updates_a_stored_one_is_never_capped():
    stored = [{"subject": "Ines", "fact": "Ines lives in Lisbon."}]
    facts = [fact(f"Me owns {n} bicycles.") for n in range(2, 5)]
    facts.append(fact("Ines lives in Porto now.", "personal", 30, subject="Ines"))
    out = summarize(facts, max_facts=3, known=lambda fs: stored)
    assert "Ines lives in Porto now." in texts(out) and len(out["facts"]) == 4
    # a failing lookup only means the cap applies to everything
    def broken(fs):
        raise RuntimeError("db locked")
    assert len(summarize(facts, max_facts=3, known=broken)["facts"]) == 3


def test_a_re_summary_keeps_a_fact_that_replaced_another(pipeline):
    store = pipeline.store
    old = conversation(store, "c0", "Ines lives in Lisbon.",
                       [{"subject": "Ines", "fact": "Ines lives in Lisbon.",
                         "source_at": T0 + 1}])
    conv = conversation(store, "c1", "Ines moved to Porto.",
                        [{"subject": "Ines", "fact": "Ines lives in Porto now.",
                          "source_at": T0 + 1}])
    new_id = json_facts(pipeline)["Ines lives in Porto now."]["id"]
    old_id = json_facts(pipeline)["Ines lives in Lisbon."]["id"]
    store.link_fact(new_id, "updates", old_id)
    known = store.facts_like(conv, [{"subject": "Ines", "fact": "Ines lives in Porto now."}])
    assert {"Ines lives in Porto now.", "Ines lives in Lisbon."} <= {k["fact"] for k in known}
    assert old != conv


def test_a_fact_said_again_with_a_kind_gives_it_to_the_old_one(pipeline):
    store = pipeline.store
    conversation(store, "c0", "Oat milk for me.",
                 [{"subject": "Me", "fact": "Me drinks oat milk.", "source_at": T0 + 1}])
    conversation(store, "c1", "Oat milk, as always.",
                 [{"subject": "Me", "fact": "Me drinks oat milk.", "kind": "preference",
                   "source_at": T0 + 1}])
    ids = sorted(r["id"] for r in store.list_facts())
    store.link_fact(ids[1], "same", ids[0])
    assert json_facts(pipeline)["Me drinks oat milk."]["kind"] == "preference"


def segments_conversation(store, name: str, segs: list[Segment], facts: list[dict]) -> str:
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=T0, duration=30,
                    codec="wav", audio_path=None, speech=[])
    store.replace_segments(name, "omi", segs, [(T0, T0 + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments(name)])
    store.save_summary(conv, {"title": name, "summary": "s", "key_points": [],
                              "topics": [], "people": [], "action_items": [],
                              "facts": facts})
    return conv


def test_the_quote_is_the_cited_line_not_the_one_before(pipeline):
    # [9] at 9.7 s and [10] at 10.6 s: the fact cites [10] (source_at = start + 10)
    segs = [Segment(T0, T0 + 2, "Hi there.", "Me", True),
            Segment(T0 + 9.7, T0 + 9.9, "Right, okay.", "Priya", False),
            Segment(T0 + 10.6, T0 + 13, "My sister Ines lives in Porto.", "Me", True)]
    segments_conversation(pipeline.store, "c1", segs,
                          [{"subject": "Me", "fact": "Me's sister Ines lives in Porto.",
                            "source_at": T0 + 10}])
    assert json_facts(pipeline)["Me's sister Ines lives in Porto."]["quote"] == \
        "My sister Ines lives in Porto."


def test_the_quote_of_a_shared_second_is_the_line_with_the_fact(pipeline):
    segs = [Segment(T0, T0 + 2, "Hi there.", "Me", True),
            Segment(T0 + 10.1, T0 + 10.5, "Yeah.", "Priya", False),
            Segment(T0 + 10.7, T0 + 13, "I'm allergic to shellfish.", "Me", True)]
    segments_conversation(pipeline.store, "c1", segs,
                          [{"subject": "Me", "fact": "Me is allergic to shellfish.",
                            "source_at": T0 + 10}])
    assert json_facts(pipeline)["Me is allergic to shellfish."]["quote"] == \
        "I'm allergic to shellfish."


def test_a_wrongly_cited_line_is_quoted_as_erase_fact_would_redact_it(pipeline):
    segs = [Segment(T0, T0 + 2, "Hello.", "Me", True),
            Segment(T0 + 2.2, T0 + 5, "My sister Ines lives in Porto.", "Me", True)]
    segments_conversation(pipeline.store, "c1", segs,
                          [{"subject": "Me", "fact": "Me's sister Ines lives in Porto.",
                            "source_at": T0}])
    row = json_facts(pipeline)["Me's sister Ines lives in Porto."]
    assert row["quote"] == "My sister Ines lives in Porto."
    pipeline.store.erase_fact(row["id"])
    texts_left = [s.text for s in pipeline.store.conversation_segments(row["conversation_id"])]
    assert "Hello." in texts_left and "My sister Ines lives in Porto." not in texts_left
