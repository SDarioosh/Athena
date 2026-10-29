"""Voice notes: intentions, priorities, principles and decisions are facts about the owner,
not action items. "I should stop prioritizing side projects ..." once gave four to-dos;
the prompts say so and code moves any the model still lists into the facts."""

from __future__ import annotations

import pytest

from athena.domain import Segment
from athena.summarize.client import LLMClient
from athena.summarize.prompts import ACTIONS_SYSTEM, FACTS_SYSTEM, NOTE_ACTIONS_SYSTEM
from athena.summarize.summarizer import Summarizer
from tests.conftest import fake_llm_transport

T0 = 1_760_000_000.0  # Thursday 2025-10-09

# fictional voice note, and the four to-dos a small model wrote for it
SIDE_PROJECTS = ("I should stop prioritizing side projects like the e-paper PDA before the "
                 "base system is stable. Focus on testing existing bugs before adding "
                 "features. Figure out how it could use an acceptable amount of battery.")
SIDE_PROJECT_ITEMS = [
    {"text": "I should stop prioritizing side projects like the e-paper PDA before the "
             "base system is stable.", "owner": "Me", "at_seconds": 0},
    {"text": "Stabilize the base system before working on side projects", "owner": "Me",
     "at_seconds": 0},
    {"text": "Focus on testing existing bugs before adding features.", "owner": "Me",
     "at_seconds": 0},
    {"text": "Figure out how it could use an acceptable amount of battery.", "owner": "Me",
     "at_seconds": 0},
]


def summarize(items: list[dict], said: str, facts: list[dict] | None = None,
              kind: str = "note") -> dict:
    summary = {"title": "Plans", "summary": "s", "key_points": [], "people": [],
               "topics": [], "facts": facts or [], "action_items": items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    return Summarizer(llm, "Me", timezone="Europe/Berlin").summarize(
        [Segment(T0, T0 + 5, said, "Me", True)], kind=kind)


def test_decisions_and_plans_become_facts():
    out = summarize(SIDE_PROJECT_ITEMS, SIDE_PROJECTS)
    assert out["action_items"] == []
    facts = [f["fact"] for f in out["facts"]]
    assert 3 <= len(facts) <= 4
    assert {f["subject"] for f in out["facts"]} == {"Me"}
    assert ("The owner decided to stop prioritizing side projects like the e-paper PDA "
            "before the base system is stable.") in facts
    assert "The owner decided to focus on testing existing bugs before adding features." \
        in facts
    assert "The owner plans to figure out how it could use an acceptable amount of " \
        "battery." in facts
    assert all(f["source_at"] == T0 for f in out["facts"])


def test_a_fact_the_model_wrote_is_not_repeated():
    known = {"subject": "Me", "fact": "The owner decided to focus on testing existing bugs "
                                      "before adding features.", "at_seconds": 0}
    out = summarize([SIDE_PROJECT_ITEMS[2]], SIDE_PROJECTS, facts=[known])
    assert out["action_items"] == [] and [f["fact"] for f in out["facts"]] == [known["fact"]]


@pytest.mark.parametrize("said,item,kind,list_name", [
    # a reminder with a day and a command word
    ("Remind me Friday to send the slides.",
     {"text": "Send the slides", "kind": "reminder", "due": "Friday",
      "title": "Send the slides"}, "reminder", None),
    # a named list
    ("Add milk to my shopping list.", {"text": "Add milk to my shopping list"}, "task",
     "shopping"),
    # a concrete verb and object
    ("Call the plumber about the tap.", {"text": "Call the plumber about the tap",
                                         "owner": "Me"}, "task", None),
    # an intent word with a concrete to-do stays a to-do
    ("I should email Bob about the invoice.",
     {"text": "Email Bob about the invoice", "owner": "Me"}, "task", None),
    # a command word keeps even a principle on the to-do list
    ("Todo: stop prioritizing side projects.", {"text": "Stop prioritizing side projects"},
     "task", None),
    # a date keeps it too
    ("I should stop prioritizing side projects from Monday.",
     {"text": "Stop prioritizing side projects", "due": "Monday"}, "task", None),
    # someone else's to-do is not the owner's decision
    ("Bob should focus on the bugs first.",
     {"text": "Focus on the bugs first", "owner": "Bob"}, "task", None),
])
def test_to_dos_stay_to_dos(said, item, kind, list_name):
    (got,) = summarize([item], said)["action_items"]
    assert (got["kind"], got["list_name"]) == (kind, list_name)


@pytest.mark.parametrize("said,item,fact", [
    ("I need to sleep more.", "Sleep more", "The owner decided to sleep more."),
    ("I should be more patient with the team.", "Be more patient with the team",
     "The owner decided to be more patient with the team."),
    ("From now on I'll test every pull request before merging it.",
     "From now on, test every pull request before merging it",
     "The owner decided to test every pull request before merging it."),
    ("We decided to move the launch to spring.", "We decided to move the launch to spring",
     "The owner decided to move the launch to spring."),
    ("Think about how the relay could reconnect faster.",
     "Think about how the relay could reconnect faster",
     "The owner plans to think about how the relay could reconnect faster."),
])
def test_principles_and_vague_wishes(said, item, fact):
    out = summarize([{"text": item, "owner": "Me"}], said)
    assert out["action_items"] == []
    assert [(f["subject"], f["fact"]) for f in out["facts"]] == [("Me", fact)]


def test_one_clause_does_not_move_the_other():
    said = "I should stop prioritizing side projects and call the plumber about the tap."
    items = [{"text": "Stop prioritizing side projects"},
             {"text": "Call the plumber about the tap"}]
    out = summarize(items, said)
    assert [i["text"] for i in out["action_items"]] == ["Call the plumber about the tap"]
    assert [f["fact"] for f in out["facts"]] == [
        "The owner decided to stop prioritizing side projects."]


def test_conversations_keep_their_items():
    out = summarize(SIDE_PROJECT_ITEMS[2:3], SIDE_PROJECTS, kind="casual")
    assert [i["text"] for i in out["action_items"]] == [
        "Focus on testing existing bugs before adding features."]


def test_prompts_say_what_is_an_action_item():
    for prompt in (ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM):
        for words in ("concrete thing to do", "I should focus on X before Y",
                      "stop prioritizing Z", "we decided to", "from now on",
                      "NOT an action item", "figure out how it could use less battery",
                      "remind me"):
            assert words in prompt
    facts = FACTS_SYSTEM.format(owner="Me")
    assert '"The owner decided to stabilise the base system before side projects."' in facts
    assert '"subject": "Me"' in facts and "The owner plans to" in facts
