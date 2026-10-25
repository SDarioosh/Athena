"""Regression tests for the QC report of 2026-10-25 (action items and beliefs). Each test
failed on the code before its fix; see docs/qc-report-2026-10-25.md. Fictional data."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from test_confirmed_facts import DAY, T0
from test_erase_fact import conversation
from test_mcp import call

from convo_server.dates import resolve_due
from convo_server.llm import LLMClient, Summarizer, _list_and_text
from convo_server.mcp_server import build_mcp
from convo_server.store import ERASED_TEXT, Segment, Store


def summary(**fields) -> dict:
    out = {"title": "T", "summary": "s", "key_points": [], "topics": [], "people": [],
           "facts": [], "action_items": []}
    out.update(fields)
    return out


def recorded(store: Store, name: str, start: float, lines: list[tuple]) -> str:
    """A conversation of ``lines`` ((offset, seconds, text[, speaker])) at ``start``."""
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                    duration=60, codec="wav", audio_path=None, speech=[])
    store.replace_segments(name, "omi", [
        Segment(start + at, start + at + n, text, *(rest or ("Bob",)))
        for at, n, text, *rest in lines], [(start, start + 60)])
    return store.save_conversation(None, "omi",
                                   [s.id for s in store.session_segments(name)])


@pytest.fixture
def store(pipeline) -> Store:
    return pipeline.store


# --------------------------------------------------------------- action items
def test_a_new_item_in_a_resummary_never_takes_an_id_an_old_item_keeps(store):
    """BUG-01: SQLite gave the first, new item the id of an old item that the next one
    kept, and the re-summary failed with UNIQUE constraint failed: action_items.id."""
    conv = recorded(store, "s", T0, [(0, 4, "hello")])
    store.save_summary(conv, summary(action_items=[{"text": "Call the plumber"},
                                                   {"text": "Water the plants"}]))
    first = {i["text"]: i["id"] for i in store.list_action_items(None)}
    store.save_summary(conv, summary(action_items=[{"text": "Book the dentist"},
                                                   {"text": "Call the plumber"}]))
    after = {i["text"]: i["id"] for i in store.list_action_items(None)}
    assert after["Call the plumber"] == first["Call the plumber"]
    assert after["Book the dentist"] > max(first.values())


def test_an_edited_note_title_survives_a_resummary(store):
    """BUG-04: the user's note title went back to the model's after a re-summary."""
    conv = recorded(store, "s", T0, [(0, 4, "Make a note: screws for the shelf")])
    item = {"text": "Buy screws", "kind": "note", "note_title": "Shelf"}
    store.save_summary(conv, summary(action_items=[item]))
    (row,) = store.list_action_items(notes=True)
    store.update_action_item(row["id"], title="Hardware store")
    store.save_summary(conv, summary(action_items=[item]))
    after = store.get_action_item(row["id"])
    assert (after["title"], after["note_title"]) == ("Hardware store", "Hardware store")


def test_an_unedited_note_takes_the_new_note_title(store):
    conv = recorded(store, "s", T0, [(0, 4, "Make a note: screws for the shelf")])
    store.save_summary(conv, summary(action_items=[
        {"text": "Buy screws", "kind": "note", "note_title": "Shelf"}]))
    store.save_summary(conv, summary(action_items=[
        {"text": "Buy screws", "kind": "note", "note_title": "Shelf screws"}]))
    (row,) = store.list_action_items(notes=True)
    assert row["note_title"] == "Shelf screws"


def test_clearing_a_note_title_clears_note_title_too(store):
    """BUG-10: title='none' gave the title back from the text but left note_title on the
    old custom name, which is what the hub shows."""
    conv = recorded(store, "s", T0, [(0, 4, "Make a note: screws")])
    store.save_summary(conv, summary(action_items=[
        {"text": "Buy screws", "kind": "note", "note_title": "Shelf"}]))
    (row,) = store.list_action_items(notes=True)
    store.update_action_item(row["id"], title="Custom")
    store.update_action_item(row["id"], title=None)
    after = store.get_action_item(row["id"])
    assert after["note_title"] == after["title"] == "Buy screws"


def test_clearing_a_task_title_leaves_note_title_alone(store):
    conv = recorded(store, "s", T0, [(0, 4, "hello")])
    store.save_summary(conv, summary(action_items=[{"text": "Call the plumber"}]))
    (row,) = store.list_action_items()
    store.update_action_item(row["id"], title="Plumber")
    store.update_action_item(row["id"], title=None)
    assert store.get_action_item(row["id"])["note_title"] is None


# ------------------------------------------------------------------ MCP tools
@pytest.fixture
def mcp(pipeline):
    store = pipeline.store
    conv = recorded(store, "s", T0, [(0, 4, "Lunch with Carol on Friday", "Carol"),
                                     (10, 4, "I'll bring the slides", "Me", True)])
    store.save_summary(conv, summary(people=["Carol"], action_items=[
        {"text": "Bring the slides", "owner": "Me"},
        {"text": "Send Dave the invoice", "owner": "Me"}]))
    return build_mcp(store, pipeline.settings)


@pytest.mark.parametrize("tool", ["list_action_items", "search_transcripts",
                                  "search_conversations"])
def test_a_bad_date_is_an_answer_not_a_tool_error(mcp, tool):
    """BUG-05: since='yesterday' raised out of the tool (UnexpectedToolError)."""
    assert call(mcp, tool, since="yesterday") == "bad date 'yesterday'; use YYYY-MM-DD"
    assert call(mcp, tool, until="last week").startswith("bad date 'last week'")


@pytest.mark.parametrize("status", ["Open", "OPEN", " open ", "ALL", "All"])
def test_action_item_status_ignores_case(mcp, status):
    """BUG-06: status='Open' or 'ALL' found nothing."""
    assert "Bring the slides" in call(mcp, "list_action_items", status=status)


def test_an_unknown_action_item_status_is_named(mcp):
    assert call(mcp, "list_action_items", status="pending") == (
        "status must be open, done, dismissed or all")


def test_person_finds_items_of_a_conversation_with_them(mcp):
    """BUG-07: person= only looked at the owner, so the to-dos of a lunch with Carol
    (owner Me) weren't found for Carol."""
    out = call(mcp, "list_action_items", person="Carol")
    assert "Bring the slides" in out and "Send Dave the invoice" in out
    rows = json.loads(call(mcp, "list_action_items", person="carol", format="json"))
    assert {r["text"] for r in rows} == {"Bring the slides", "Send Dave the invoice"}


def test_person_finds_items_that_name_them_and_items_they_own(mcp):
    assert "Send Dave the invoice" in call(mcp, "list_action_items", person="Dave")
    assert "Bring the slides" not in call(mcp, "list_action_items", person="Dave")
    assert "Bring the slides" in call(mcp, "list_action_items", person="Me")
    assert call(mcp, "list_action_items", person="Zed") == "No open action items found."


# -------------------------------------------------------------------- beliefs
def _fact(store: Store, name: str, start: float, subject: str, fact: str) -> int:
    conv = recorded(store, name, start, [(0, 4, fact)])
    (fid,) = store.save_summary(conv, summary(facts=[
        {"subject": subject, "fact": fact, "source_at": start}]))
    return fid


def test_links_that_arrive_out_of_order_still_end_at_the_current_fact(store):
    """BUG-02: "Tokyo updates London" then "London updates Paris" left Paris pointing at
    London (itself replaced), so a search for Paris found nothing."""
    paris = _fact(store, "c1", T0, "Alice", "Alice lives in Paris")
    london = _fact(store, "c2", T0 + DAY, "Alice", "Alice moved to London")
    tokyo = _fact(store, "c3", T0 + 2 * DAY, "Alice", "Alice moved to Tokyo")
    store.link_fact(tokyo, "updates", london)
    store.link_fact(london, "updates", paris)
    assert [f["id"] for f in store.list_facts(query="Paris")] == [tokyo]
    assert [f["id"] for f in store.list_facts(query="London")] == [tokyo]
    assert {f["id"] for f in store.fact_history(tokyo)} == {paris, london}


def test_a_chain_left_by_older_data_is_followed(store):
    """Rows already stored as a chain (before the link fix) resolve to the end too."""
    paris = _fact(store, "c1", T0, "Alice", "Alice lives in Paris")
    london = _fact(store, "c2", T0 + DAY, "Alice", "Alice moved to London")
    tokyo = _fact(store, "c3", T0 + 2 * DAY, "Alice", "Alice moved to Tokyo")
    with store.tx() as c:
        c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                  (london, paris))
        c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                  (tokyo, london))
    assert [f["id"] for f in store.list_facts(query="Paris")] == [tokyo]


def test_a_fact_search_is_not_crowded_out_by_transcripts(store):
    """BUG-08: list_facts(query) took the 200 best search hits of every kind, so 200
    conversations about coffee hid the one fact about coffee."""
    for i in range(205):
        conv = recorded(store, f"s{i}", T0 + i * 100, [(0, 4, "Drinking coffee coffee")])
        store.save_summary(conv, summary(title="Coffee", summary="Coffee talk",
                                         topics=["coffee"]))
    fid = _fact(store, "f", T0 + DAY, "Carol", "Carol drinks black coffee")
    assert [f["id"] for f in store.list_facts(query="coffee")] == [fid]


def test_related_facts_are_not_crowded_out_by_transcripts(store):
    """Same cause in related_facts: the 5 best hits of every kind, so a known fact was
    not offered for linking when transcripts matched its words better."""
    known = _fact(store, "k", T0, "Carol", "Carol drinks black coffee")
    for i in range(10):
        conv = recorded(store, f"s{i}", T0 + DAY + i * 100,
                        [(0, 4, "Carol drinks black coffee, black coffee")])
        store.save_summary(conv, summary(title="Carol coffee black",
                                         summary="Carol black coffee"))
    conv = recorded(store, "new", T0 + 2 * DAY, [(0, 4, "Carol only drinks black coffee")])
    new = store.save_summary(conv, summary(facts=[
        {"subject": "Coffee", "fact": "Carol only drinks black coffee", "source_at": T0}]))
    facts = store.facts_by_id(new)
    assert known in {f["id"] for f in store.related_facts(conv, facts)}


# ------------------------------------------------------------- erase_fact lines
def test_erase_redacts_the_line_that_says_the_fact_when_the_model_cites_the_next(store):
    """BUG-03: the model cited the line after the one with the fact; that innocent line
    was redacted and the fact's own words stayed."""
    conv = recorded(store, "s", T0, [(0, 5, "The gate code is 4417, Alice"),
                                     (6, 4, "Let us talk about the weather")])
    (fid,) = store.save_summary(conv, summary(facts=[
        {"subject": "Alice", "fact": "The gate code is 4417", "source_at": T0 + 6}]))
    assert store.erase_fact(fid)["lines"] == 1
    assert [s.text for s in store.conversation_segments(conv)] == [
        ERASED_TEXT, "Let us talk about the weather"]


def test_erase_still_redacts_a_cited_line_that_only_paraphrases(store):
    conv = recorded(store, "s", T0, [(0, 4, "Shall we get lunch"),
                                     (10, 4, "She doesn't eat meat at all"),
                                     (20, 4, "The taqueria then")])
    (fid,) = store.save_summary(conv, summary(facts=[
        {"subject": "Alice", "fact": "Alice is vegetarian", "source_at": T0 + 10}]))
    store.erase_fact(fid)
    assert [s.text for s in store.conversation_segments(conv)] == [
        "Shall we get lunch", ERASED_TEXT, "The taqueria then"]


def test_erase_finds_the_line_of_a_fact_with_no_source_offset(store):
    conv = recorded(store, "s", T0, [(0, 4, "Shall we get lunch"),
                                     (10, 4, "The gate code is 4417")])
    (fid,) = store.save_summary(conv, summary(facts=[
        {"subject": "Me", "fact": "The gate code is 4417", "source_at": None}]))
    assert store.erase_fact(fid)["lines"] == 1
    assert [s.text for s in store.conversation_segments(conv)] == [
        "Shall we get lunch", ERASED_TEXT]


def test_erase_never_redacts_a_far_line_that_shares_words(store):
    conv = recorded(store, "s", T0, [(0, 4, "Book the gate code workshop"),
                                     (50, 4, "Something else entirely")])
    (fid,) = store.save_summary(conv, summary(facts=[
        {"subject": "Me", "fact": "The gate code is 4417", "source_at": T0 + 50}]))
    store.erase_fact(fid)
    assert [s.text for s in store.conversation_segments(conv)] == [
        "Book the gate code workshop", ERASED_TEXT]


# ---------------------------------------------------- not reproduced: guards
def test_a_failing_forget_rewrite_does_not_block_the_others(pipeline):
    """BUG-09 (not reproduced): a rewrite that keeps failing moves behind the others
    (pending_rewrites orders by attempts), so the next pass rewrites them."""
    store = pipeline.store
    conversation(store, "a", T0, fact="Alice is vegetarian")
    cb, fb = conversation(store, "b", T0 + DAY, fact="Bob is allergic to nuts")
    store.erase_fact(store.list_facts(query="vegetarian")[0]["id"])
    store.erase_fact(fb)

    def handler(request: httpx.Request) -> httpx.Response:
        if "vegetarian" in request.content.decode():
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"title": "Lunch", "summary": "They planned lunch.", "key_points": [],
             "topics": []})}}]})

    pipeline.summarizer = Summarizer(LLMClient(
        "http://llm.test/v1", "m", transport=httpx.MockTransport(handler)), "Me")
    pipeline.rewrite_forgotten()  # the older one fails first and stops the pass
    assert pipeline.rewrite_forgotten() == 1
    assert cb not in {r["conversation_id"] for r in store.pending_rewrites()}


# ------------------------------------------------------------- dates and lists
@pytest.mark.parametrize("phrase,want", [
    ("in 3 days", date(2026, 10, 28)), ("in three days", date(2026, 10, 28)),
    ("in a week", date(2026, 11, 1)), ("in two weeks", date(2026, 11, 8)),
    ("remind me in 10 days", date(2026, 11, 4)), ("in 2 weeks' time", date(2026, 11, 8)),
    ("in a day or two", None), ("in 3 days or so", None), ("in a day", None),
    ("in an hour", None), ("in 400 days", None),
])
def test_resolve_due_relative_offsets(phrase, want):
    """BUG-13: "in 3 days" / "in a week" gave no date (the model's guess was kept)."""
    assert resolve_due(phrase, date(2026, 10, 25)) == want


@pytest.mark.parametrize("text", ["Please add milk to my shopping list",
                                  "Can you add milk to the shopping list",
                                  "Could you please put milk on my shopping list",
                                  "Please, add milk to the shopping list"])
def test_a_polite_add_command_names_its_list(text):
    """BUG-14: with "Please"/"Can you" in front, the list wasn't read from the text and
    the item kept the whole command."""
    assert _list_and_text(text, None) == ("Milk", "shopping")
    assert _list_and_text(text, "shopping") == ("Milk", "shopping")
