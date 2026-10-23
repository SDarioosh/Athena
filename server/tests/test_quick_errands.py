"""Quick errands are not action items, and a possessive is not part of a name.

"I'll move my truck" said in passing once became the suggested to-do "Walk truck to
Marisol's house", shown "with Marisol's". The prompts say what a quick errand is; code
drops the ones whose clause says they happen right now, and strips "'s" from people's names
(new summaries and, once, the names already stored). Fictional data."""

from __future__ import annotations

import json

import pytest
from conftest import fake_llm_transport
from test_confirmed_facts import HOUR, T0, say

from convo_server.llm import ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM, LLMClient, Summarizer
from convo_server.store import Segment, Store, person_name


def summarize(items: list[dict], lines: list[str], kind: str = "casual",
              people: list[str] | None = None) -> dict:
    summary = {"title": "Chat", "summary": "s", "key_points": [], "people": people or [],
               "topics": [], "facts": [], "action_items": items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    segments = [Segment(T0 + 10 * i, T0 + 10 * i + 5, text, "Me", True)
                for i, text in enumerate(lines)]
    return Summarizer(llm, "Me", timezone="Europe/Berlin").summarize(segments, kind=kind)


def test_prompts_say_a_quick_errand_is_not_an_action_item():
    for prompt in (ACTIONS_SYSTEM, NOTE_ACTIONS_SYSTEM):
        assert "I'll move my truck" in prompt and "nobody needs reminding" in prompt
    assert "small ones too" not in ACTIONS_SYSTEM
    assert "keep the verb" in ACTIONS_SYSTEM


@pytest.mark.parametrize("said,item", [
    ("Let me move my truck right now so you can get out.", "Move the truck"),
    ("I'll grab the keys real quick.", "Grab the keys"),
    ("Hold on, I'll get the door in a second.", "Get the door"),
    ("I'm going to move my car right now.", "Move the car"),
])
@pytest.mark.parametrize("kind", ["casual", "note"])
def test_quick_errands_are_dropped(said, item, kind):
    out = summarize([{"text": item, "owner": "Me", "at_seconds": 0}], [said], kind=kind)
    assert out["action_items"] == []


def test_a_reworded_errand_is_found_by_its_line():
    # the model changed the verb, so the item's words don't find the clause: the line
    # it cites does
    out = summarize([{"text": "Walk truck to Marisol's house for her", "owner": "Me",
                      "at_seconds": 10}],
                    ["Thanks for coming over.", "I'll move my truck to your place right now."])
    assert out["action_items"] == []


def test_a_line_of_several_clauses_is_not_guessed():
    out = summarize([{"text": "Email Ana the invoice", "owner": "Me", "at_seconds": 0}],
                    ["Let me sit down right now, and I'll mail her the bill."])
    assert [i["text"] for i in out["action_items"]] == ["Email Ana the invoice"]


@pytest.mark.parametrize("said,item", [
    # a promise for later
    ("I'll send you the slides tonight.", {"text": "Send Sam the slides", "due": "tonight"}),
    # no "now"
    ("I'll text you the address.", {"text": "Text Sam the address"}),
    ("Let me check with my wife and get back to you.", {"text": "Check with wife"}),
    # a date keeps it
    ("Let me call the plumber on Friday.", {"text": "Call the plumber", "due": "Friday"}),
    # a command word keeps it
    ("Remind me to move the truck right now.", {"text": "Move the truck"}),
    # a named list keeps it
    ("Let me add milk to my shopping list.", {"text": "Add milk to my shopping list"}),
])
def test_to_dos_for_later_stay(said, item):
    out = summarize([{**item, "owner": "Me", "at_seconds": 0}], [said])
    assert len(out["action_items"]) == 1


@pytest.mark.parametrize("name,want", [
    ("Marisol's", "Marisol"), ("Ana’s", "Ana"), ("James'", "James"), ("Ross", "Ross"),
    (" Lena ", "Lena"), ("O'Neil", "O'Neil"),
])
def test_person_name(name, want):
    assert person_name(name) == want


def test_summary_people_lose_the_possessive():
    out = summarize([], ["Marisol lent me her truck for the move."],
                    people=["Marisol's", "Marisol", "Bob"])
    assert out["people"] == ["Marisol", "Bob"]


def test_migration_fixes_stored_possessives(tmp_path):
    path = tmp_path / "c.db"
    store = Store(path)
    c0, _ = say(store, "c0", T0, "Bob", "Bob lives in Berlin", people=("Marisol's", "Bob"))
    c1, _ = say(store, "c1", T0 + HOUR, "Ana", "Ana is vegetarian", people=("Ana", "Ana's"))
    old = store.person_id("Marisol's")
    ana = store.person_id("Ana")
    store._conn.execute("PRAGMA user_version = 11")
    store.close()

    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 13
    # renamed in place: the id survives, the old spelling is an alias
    assert store.person_id("Marisol") == old == store.person_id("Marisol's")
    # "Ana's" merges into the Ana already there
    assert store.person_id("Ana's") == ana
    names = {r[0] for r in store._conn.execute("SELECT name FROM conversation_people")}
    assert names == {"Marisol", "Bob", "Ana"}
    people = {r["id"]: json.loads(r["people"])
              for r in store._conn.execute("SELECT id, people FROM conversations")}
    assert people[c0] == ["Marisol", "Bob"] and people[c1] == ["Ana"]
    live = {r[0] for r in store._conn.execute(
        "SELECT name FROM people WHERE merged_into IS NULL")}
    assert "Marisol's" not in live and "Ana's" not in live
