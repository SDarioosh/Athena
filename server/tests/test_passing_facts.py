"""Facts are for months: one tied to a single day is dropped.

"Marisol was very focused and locked in today." and "Marisol did not drink enough water
today." reached the hub's memory review as beliefs. The facts prompt names these, and
code drops any fact that says it is about one day ("today", "tonight", "this morning",
"right now").

"Ana is interested in hearing how the voice compression technology sounds." was true
when said, but it is passing curiosity, and "the technology" means nothing without the
conversation. The prompt asks for facts that name what they are about and skips
curiosity; code drops curiosity about how or what something is. Fictional data."""

from __future__ import annotations

import pytest
from conftest import fake_llm_transport
from test_confirmed_facts import T0

from athena.llm import FACTS_SYSTEM, LLMClient, Summarizer
from athena.store import Segment


def summarize(facts: list[str], kind: str = "casual") -> list[str]:
    summary = {"title": "Chat", "summary": "s", "key_points": [], "people": [],
               "topics": [], "action_items": [],
               "facts": [{"subject": "Ana", "fact": f, "at_seconds": 0} for f in facts]}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    segments = [Segment(T0 + 10 * i, T0 + 10 * i + 5, "We talked about a few things.", "Me", True)
                for i in range(8)]
    out = Summarizer(llm, "Me", timezone="Europe/Berlin").summarize(segments, kind=kind)
    return [f["fact"] for f in out["facts"]]


def test_prompt_names_one_day_states_and_moment_plans():
    for words in ("was very focused today", "did not drink enough water", "get a snack",
                  "still matter in a month"):
        assert words in FACTS_SYSTEM


@pytest.mark.parametrize("kind", ["casual", "note"])
def test_one_day_facts_are_dropped(kind):
    got = summarize(["Ana was very focused and locked in today.",
                     "Ana did not drink enough water today.",
                     "Ana is tired this morning.",
                     "Ana is cooking dinner right now.",
                     "Ana is allergic to peanuts.",
                     "Ana plans to move to Lisbon next spring."], kind=kind)
    assert got == ["Ana is allergic to peanuts.", "Ana plans to move to Lisbon next spring."]


def test_words_inside_other_words_are_not_one_day():
    got = summarize(["Ana writes for the Todayish blog.", "Ana works nights as a nurse."])
    assert got == ["Ana writes for the Todayish blog.", "Ana works nights as a nurse."]


def test_prompt_asks_for_named_things_and_skips_curiosity():
    for words in ("passing curiosity", "months later without the conversation",
                  '"the technology"', "leave the fact out"):
        assert words in FACTS_SYSTEM


@pytest.mark.parametrize("kind", ["casual", "note"])
def test_curiosity_facts_are_dropped(kind):
    got = summarize(["Ana is interested in hearing how the voice compression technology sounds.",
                     "Ana is curious to see what the new dashboard looks like.",
                     "Ana wants to know whether the demo worked.",
                     "Ana was really keen to find out how the prototype performs.",
                     "Ana is interested in learning Portuguese.",
                     "Ana wants to see Japan in cherry blossom season.",
                     "Ana plans to move to Lisbon next spring."], kind=kind)
    assert got == ["Ana is interested in learning Portuguese.",
                   "Ana wants to see Japan in cherry blossom season.",
                   "Ana plans to move to Lisbon next spring."]
