"""list_action_items JSON ``with``: the other people in the item's conversation, for the
hub's open loops ("you told Alice you'd send the file"). Fictional data."""

from __future__ import annotations

import json

from conftest import ToneEmbedder, tone
from test_confirmed_facts import HOUR, T0
from test_mcp import call

from convo_server.mcp_server import build_mcp
from convo_server.store import Segment, Store


def converse(store: Store, name: str, start: float, lines: list[tuple[str, str]],
             items: list[dict], people: tuple[str, ...] = ()) -> str:
    """A conversation ``name`` whose lines are (speaker, text) and whose summary has
    these action items and people."""
    store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                    duration=30, codec="wav", audio_path=None, speech=[])
    segs = [Segment(start + i * 2, start + i * 2 + 1, text, speaker=spk,
                    is_owner=spk == "Me") for i, (spk, text) in enumerate(lines)]
    store.replace_segments(name, "omi", segs, [(start, start + 30)])
    conv = store.save_conversation(None, "omi", [s.id for s in store.session_segments(name)])
    store.save_summary(conv, {"title": name, "summary": "", "key_points": [], "topics": [],
                              "people": list(people), "action_items": items, "facts": []})
    return conv


def items_json(pipeline) -> list[dict]:
    mcp = build_mcp(pipeline.store, pipeline.settings)
    return json.loads(call(mcp, "list_action_items", status="all", format="json"))


def by_text(rows: list[dict]) -> dict[str, list[dict]]:
    return {r["text"]: r["with"] for r in rows}


def test_with_lists_speakers_then_summary_people_text_named_first(pipeline):
    store = pipeline.store
    converse(store, "c0", T0, [("Me", "I'll send the file"), ("Bob", "Thanks"),
                               ("Alice", "And the slides?"), ("Speaker 2", "Hm"),
                               ("Unknown", "...")],
             [{"text": "Send the slides to alice", "owner": "Me"},
              {"text": "Book a room", "owner": "Me"}],
             people=("Carol", "Me", "Bob", "Speaker 3"))
    for name in ("Alice", "Bob"):
        store.ensure_person(name)
    ids = {n: store.person_id(n) for n in ("Alice", "Bob", "Carol")}
    rows = by_text(items_json(pipeline))
    # unnamed labels and the owner are never listed; no duplicates (Bob spoke and is listed)
    assert rows["Book a room"] == [{"id": ids["Bob"], "name": "Bob"},
                                   {"id": ids["Alice"], "name": "Alice"},
                                   {"id": ids["Carol"], "name": "Carol"}]
    # the person the item names comes first (case-insensitive word match)
    assert [p["name"] for p in rows["Send the slides to alice"]] == ["Alice", "Bob", "Carol"]


def test_word_match_only_and_ids_resolve(pipeline):
    store = pipeline.store
    converse(store, "c0", T0, [("Al", "hi"), ("Alice", "hello")],
             [{"text": "Call Alice back", "owner": "Me"}])
    store.ensure_person("alice")
    # "Al" isn't a word in "Alice"; a speaker with no person row has id null
    assert by_text(items_json(pipeline))["Call Alice back"] == [
        {"id": store.person_id("Alice"), "name": "alice"}, {"id": None, "name": "Al"}]


def test_owner_excluded_by_voice_name_alias_and_setting(pipeline):
    store = pipeline.store
    store.put_speaker("Sam", ToneEmbedder().embed(tone(300, 2)), is_owner=True)
    store.ensure_person("Sam", is_owner=True)
    converse(store, "c0", T0, [("Sam", "I'll do it"), ("Dana", "Great")],
             [{"text": "Email Dana and Sam", "owner": "Sam"}],
             people=("Sam", "me", "Me", "Dana"))
    settings = pipeline.settings.model_copy(update={"owner_name": "Sam"})
    out = json.loads(call(build_mcp(store, settings), "list_action_items", format="json"))
    assert [p["name"] for p in out[0]["with"]] == ["Dana"]


def test_merged_person_reports_the_surviving_id(pipeline):
    store = pipeline.store
    converse(store, "c0", T0, [("Kat", "I need the notes")],
             [{"text": "Send Kat the notes", "owner": "Me"}])
    converse(store, "c1", T0 + HOUR, [("Katherine", "Hi")], [], people=("Katherine",))
    katherine = store.person_id("Katherine")
    store.rename_everywhere("Kat", "Katherine")
    rows = by_text(items_json(pipeline))
    assert rows["Send Kat the notes"] == [{"id": katherine, "name": "Katherine"}]

    # a name only an alias of someone resolves to that person and still counts as named
    converse(store, "c2", T0 + 2 * HOUR, [("Kim", "ok")],
             [{"text": "Ask Kat about Friday", "owner": "Me"}], people=("Kat",))
    got = by_text(items_json(pipeline))["Ask Kat about Friday"]
    assert got == [{"id": katherine, "name": "Katherine"},
                   {"id": store.person_id("Kim"), "name": "Kim"}]


def test_notes_and_lonely_items_have_empty_with(pipeline):
    store = pipeline.store
    converse(store, "c0", T0, [("Me", "note this"), ("Bob", "sure")],
             [{"text": "Bob likes jazz", "kind": "note", "note_title": "Bob"},
              {"text": "Water the plants", "owner": "Me"}])
    converse(store, "c1", T0 + HOUR, [("Me", "remind me")],
             [{"text": "Buy milk", "owner": "Me"}])
    rows = by_text(items_json(pipeline))
    assert rows["Bob likes jazz"] == []
    assert rows["Buy milk"] == []
    assert [p["name"] for p in rows["Water the plants"]] == ["Bob"]


def test_text_output_unchanged(pipeline):
    store = pipeline.store
    converse(store, "c0", T0, [("Bob", "send it")], [{"text": "Send Bob the file",
                                                       "owner": "Me"}])
    out = call(build_mcp(store, pipeline.settings), "list_action_items")
    line = next(ln for ln in out.splitlines() if "Send Bob the file" in ln)
    assert line.startswith("- #") and "(owner Me)" in line
    assert '"with"' not in out and "'id'" not in out and "Bob\"}" not in out


def test_participants_are_computed_in_one_pass(pipeline, monkeypatch):
    store = pipeline.store
    for i in range(3):
        converse(store, f"c{i}", T0 + i * HOUR, [("Bob", "hi")],
                 [{"text": f"Task {i}", "owner": "Me"}, {"text": f"Other {i}", "owner": "Me"}])
    calls = []
    real = store.conversation_participants
    monkeypatch.setattr(store, "conversation_participants",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    rows = items_json(pipeline)
    assert len(rows) == 6 and len(calls) == 1
    assert all(r["with"] == [{"id": store.person_id("Bob"), "name": "Bob"}] for r in rows)
