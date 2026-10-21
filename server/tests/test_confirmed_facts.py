"""Confirmed facts (said in several conversations on several days) and list_facts' JSON
mode, which the hub reads to copy confirmed facts into its memory. Fictional data."""

from __future__ import annotations

import json

import pytest
from conftest import ToneEmbedder, tone
from test_mcp import call

from convo_server.links import transcript_link
from convo_server.mcp_server import build_mcp
from convo_server.store import Segment

T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC
HOUR, DAY = 3600.0, 86400.0


def say(store, name: str, start: float, subject: str, fact: str,
        people: tuple[str, ...] = ()) -> tuple[str, int]:
    """A conversation ``name`` starting at ``start`` whose summary has one fact."""
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                    duration=30, codec="wav", audio_path=None, speech=[])
    store.replace_segments(name, "omi", [Segment(start, start + 5, f"{fact}, really")],
                           [(start, start + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments(name)])
    (fid,) = store.save_summary(conv, {
        "title": name, "summary": fact, "key_points": [], "topics": [],
        "people": list(people), "action_items": [],
        "facts": [{"subject": subject, "fact": fact, "source_at": start + 2}]})
    return conv, fid


def said_at(store, starts: list[float], subject: str = "Alice",
            fact: str = "Alice is vegetarian") -> int:
    """The same fact said in one conversation per start time; returns the kept fact id."""
    _, first = say(store, "c0", starts[0], subject, fact)
    for i, start in enumerate(starts[1:], 1):
        _, again = say(store, f"c{i}", start, subject, fact)
        store.link_fact(again, "same", first)
    return first


def confirmed_ids(pipeline, **overrides) -> list[int]:
    settings = pipeline.settings.model_copy(update=overrides)
    out = json.loads(call(build_mcp(pipeline.store, settings), "list_facts", confirmed=True,
                          format="json"))
    return [f["id"] for f in out["facts"]]


def test_fact_said_once_is_not_confirmed(pipeline):
    said_at(pipeline.store, [T0])
    assert confirmed_ids(pipeline) == []
    text = call(build_mcp(pipeline.store, pipeline.settings), "list_facts", confirmed=True)
    assert text == "No confirmed facts found."


def test_three_conversations_on_one_day_are_not_enough(pipeline):
    said_at(pipeline.store, [T0, T0 + HOUR, T0 + 2 * HOUR])
    assert confirmed_ids(pipeline) == []


def test_three_conversations_over_two_days_confirm_a_fact(pipeline):
    fid = said_at(pipeline.store, [T0, T0 + HOUR, T0 + DAY])
    say(pipeline.store, "other", T0 + 3 * HOUR, "Bob", "Bob plays the cello")
    assert confirmed_ids(pipeline) == [fid]
    text = call(build_mcp(pipeline.store, pipeline.settings), "list_facts", confirmed=True)
    assert text.startswith("1 confirmed facts (times are UTC).")
    assert "Alice is vegetarian" in text and "cello" not in text


def test_a_replaced_fact_is_never_confirmed(pipeline):
    store = pipeline.store
    old = said_at(store, [T0, T0 + HOUR, T0 + DAY])
    assert confirmed_ids(pipeline) == [old]
    _, new = say(store, "later", T0 + 3 * DAY, "Alice", "Alice is vegan now")
    store.link_fact(new, "updates", old)
    assert confirmed_ids(pipeline) == []  # the old one is superseded, the new one said once
    assert confirmed_ids(pipeline, fact_confirm_sources=1, fact_confirm_days=1) == [new]


def test_thresholds_come_from_settings(pipeline):
    fid = said_at(pipeline.store, [T0, T0 + HOUR])
    assert confirmed_ids(pipeline) == []
    assert confirmed_ids(pipeline, fact_confirm_sources=2, fact_confirm_days=1) == [fid]
    assert confirmed_ids(pipeline, fact_confirm_sources=2, fact_confirm_days=2) == []


def test_days_are_counted_in_the_configured_timezone(pipeline):
    # 23:30 and 00:30 UTC: two dates in UTC, one evening in New York
    evening = 1_760_052_600.0  # 2025-10-09 23:30 UTC
    fid = said_at(pipeline.store, [evening, evening + HOUR])
    assert confirmed_ids(pipeline, fact_confirm_sources=2) == [fid]
    assert confirmed_ids(pipeline, fact_confirm_sources=2,
                         timezone="America/New_York") == []


def test_confirmed_filter_applies_before_the_limit(pipeline):
    store = pipeline.store
    fid = said_at(store, [T0, T0 + HOUR, T0 + DAY])
    for i in range(3):  # newer, unconfirmed facts that would fill a limit of 1
        say(store, f"new{i}", T0 + 5 * DAY + i * HOUR, "Bob", f"Bob owns {i + 2} bikes")
    out = json.loads(call(build_mcp(store, pipeline.settings), "list_facts", confirmed=True,
                          format="json", limit=1))
    assert [f["id"] for f in out["facts"]] == [fid]


def test_json_mode_shape_and_counts(pipeline):
    store = pipeline.store
    settings = pipeline.settings
    fid = said_at(store, [T0, T0 + HOUR, T0 + DAY])
    out = call(build_mcp(store, settings), "list_facts", format="json")
    data = json.loads(out)
    (fact,) = store.list_facts()
    link = transcript_link(settings, fact["conversation_id"], 2.0)
    assert data == {"facts": [{
        "id": fid, "subject": "Alice", "about": "other", "fact": "Alice is vegetarian",
        "times_said": 3, "days": 2, "first_said": "2025-10-09", "last_said": "2025-10-10",
        "link": link}]}
    assert "/view/" in link and "#t=2" in link
    assert not out.rstrip().endswith(".")  # no citation line after the JSON
    assert link in call(build_mcp(store, settings), "list_facts")  # same link as text mode


def test_json_mode_lists_current_facts_newest_first(pipeline):
    store = pipeline.store
    _, old = say(store, "a", T0, "Bob", "Bob lives in Paris")
    _, new = say(store, "b", T0 + DAY, "Bob", "Bob lives in Berlin")
    _, other = say(store, "c", T0 + 2 * HOUR, "Carol", "Carol runs marathons")
    store.link_fact(new, "updates", old)
    data = json.loads(call(build_mcp(store, pipeline.settings), "list_facts", format="json"))
    assert [f["id"] for f in data["facts"]] == [new, other]
    assert [f["times_said"] for f in data["facts"]] == [1, 1]


def test_about_classifies_owner_people_and_other(pipeline):
    store = pipeline.store
    store.put_speaker("Dana", ToneEmbedder().embed(tone(800, 2)), is_owner=False)
    facts = {
        "Me": "Me prefers tea", "  the user ": "The user works nights",
        "MYSELF": "Myself likes hiking", "alice": "Alice is vegetarian",
        "Dana": "Dana has two cats", "the Lisbon trip": "The Lisbon trip is in May",
    }
    for i, (subject, fact) in enumerate(facts.items()):
        say(store, f"c{i}", T0 + i * HOUR, subject, fact, people=("Alice",))
    data = json.loads(call(build_mcp(store, pipeline.settings), "list_facts", format="json"))
    about = {f["fact"]: f["about"] for f in data["facts"]}
    assert about == {
        "Me prefers tea": "owner", "The user works nights": "owner",
        "Myself likes hiking": "owner", "Alice is vegetarian": "person",
        "Dana has two cats": "person", "The Lisbon trip is in May": "other"}


def test_about_owner_by_enrolled_owner_name(pipeline):
    store = pipeline.store
    store.put_speaker("Sam", ToneEmbedder().embed(tone(300, 2)), is_owner=True)
    say(store, "c0", T0, "sam", "Sam is allergic to peanuts")
    (fact,) = json.loads(call(build_mcp(store, pipeline.settings), "list_facts",
                              format="json"))["facts"]
    assert fact["about"] == "owner"


def test_bad_format_is_refused(pipeline):
    assert call(build_mcp(pipeline.store, pipeline.settings), "list_facts",
                format="xml") == "format must be text or json"


@pytest.fixture
def snapshot_facts(pipeline):
    """A fact said again in a later conversation, and a fact that replaced an older one."""
    store = pipeline.store
    said_at(store, [T0, T0 + DAY])
    _, old = say(store, "old", T0 + HOUR, "Bob", "Bob lives in Paris")
    _, new = say(store, "new", T0 + 2 * DAY, "Bob", "Bob lives in Berlin")
    store.link_fact(new, "updates", old)
    return store


def test_text_mode_is_unchanged(pipeline, snapshot_facts):
    """The default text output is the same as before the JSON mode was added."""
    out = call(build_mcp(snapshot_facts, pipeline.settings), "list_facts")
    links = {c["title"]: transcript_link(pipeline.settings, c["id"], 2.0)
             for c in snapshot_facts.list_conversations()}
    assert out == "\n".join([
        "2 facts (times are UTC). When answering, put the link next to each fact you use:",
        f"- [Bob] Bob lives in Berlin (learned 2025-10-11) {links['new']}",
        f"  replaces: Bob lives in Paris (from 2025-10-09) {links['old']}",
        f"- [Alice] Alice is vegetarian (learned 2025-10-09) {links['c0']}",
        f"  said again 2025-10-10 {links['c1']}",
        "When answering, put the link next to each fact you use."])
