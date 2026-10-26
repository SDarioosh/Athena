"""People with a stable id: the hub links a person here to one of its contacts, so the
id survives renames and merges. Fictional data."""

from __future__ import annotations

import json

from conftest import ToneEmbedder, tone
from test_confirmed_facts import HOUR, T0, say
from test_mcp import call, process_two_speakers

from convo_server.mcp_server import build_mcp
from convo_server.store import Store


def voice(freq: float):
    return ToneEmbedder().embed(tone(freq, 2))


def person(store: Store, pid: int) -> dict:
    return dict(store._conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone())


def people_json(pipeline) -> list[dict]:
    out = call(build_mcp(pipeline.store, pipeline.settings), "list_people", format="json")
    return json.loads(out)["people"]


def test_migration_backfills_people_from_names_and_speakers(tmp_path):
    path = tmp_path / "c.db"
    store = Store(path)
    store.put_speaker("Sam", voice(300), is_owner=True)
    store.put_speaker("Dana", voice(800), is_owner=False)
    say(store, "c0", T0, "alice", "alice is vegetarian", people=("alice", "dana"))
    say(store, "c1", T0 + HOUR, "Bob", "Bob lives in Berlin", people=("Alice", "Bob"))
    speaker_ids = {r["name"]: r["id"] for r in store._conn.execute("SELECT * FROM speakers")}
    # a database from before people existed (schema v9)
    store._conn.execute("DELETE FROM people_aliases")
    store._conn.execute("DELETE FROM people")
    store._conn.execute("PRAGMA user_version = 9")
    store.close()

    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 14
    rows = {r["name"]: dict(r) for r in store._conn.execute("SELECT * FROM people")}
    # one row per distinct name, any case; enrolled spellings first, then the first mention
    assert set(rows) == {"Sam", "Dana", "alice", "Bob"}
    assert rows["Sam"]["speaker_id"] == speaker_ids["Sam"] and rows["Sam"]["is_owner"] == 1
    assert rows["Dana"]["speaker_id"] == speaker_ids["Dana"] and rows["Dana"]["is_owner"] == 0
    assert rows["alice"]["speaker_id"] is None and rows["alice"]["created_at"] == T0
    assert store.person_id("ALICE") == rows["alice"]["id"]
    assert store.person_id("dana") == rows["Dana"]["id"]
    assert store.owner_person_id() == rows["Sam"]["id"]
    assert store.person_id("Carol") is None


def test_every_new_name_gets_a_person(pipeline):
    store = pipeline.store
    say(store, "c0", T0, "Bob", "Bob lives in Berlin", people=("Bob", "Speaker 2"))
    bob = store.person_id("bob")
    assert bob is not None and store.ensure_person("BOB") == bob
    assert store.person_id("Speaker 2") is None  # unnamed labels are not people
    assert store.ensure_person("Speaker 1") is None and store.ensure_person("  ") is None

    store.put_speaker("Eve", voice(900), is_owner=False)  # enrollment
    assert person(store, store.person_id("Eve"))["speaker_id"] is not None
    store.add_voice_sample("Finn", voice(1000))  # a voice learned from naming
    assert person(store, store.person_id("finn"))["speaker_id"] is not None
    # an existing person gets the voice linked, keeping their id
    store.add_voice_sample("Bob", voice(1100))
    assert store.person_id("Bob") == bob and person(store, bob)["speaker_id"] is not None


def test_name_speaker_and_reassign_line_create_people(pipeline):
    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    process_two_speakers(pipeline)
    conv = store.list_conversations()[0]["id"]
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Kat")
    assert person(store, store.person_id("Kat"))["speaker_id"] is not None
    line = store.conversation_segments(conv)[0]
    call(mcp, "reassign_line", line_id=line.id, name="Dan")
    dan = store.person_id("Dan")
    assert dan is not None and person(store, dan)["speaker_id"] is None


def test_owner_follows_the_enrolled_owner_voice(pipeline):
    store = pipeline.store  # the fixture enrolls "Me" as the owner
    me = store.person_id("Me")
    assert store.owner_person_id() == me
    store.put_speaker("Sam", voice(300), is_owner=True)
    assert store.owner_person_id() == store.person_id("Sam") != me
    assert person(store, me)["is_owner"] == 0
    store.put_speaker("Sam", voice(300), is_owner=False)
    assert store.owner_person_id() is None


def test_rename_everywhere_keeps_the_id(pipeline):
    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    process_two_speakers(pipeline)
    conv = store.list_conversations()[0]["id"]
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Kat")
    kat = store.person_id("Kat")
    out = call(mcp, "rename_speaker_everywhere", old_name="kat", new_name="Kath")
    assert out.endswith(f"The person id {kat} is kept.")
    assert store.person_id("Kath") == store.person_id("KAT") == kat
    row = person(store, kat)
    assert row["name"] == "Kath" and row["speaker_id"] is not None
    (p,) = [p for p in people_json(pipeline) if p["id"] == kat]
    assert p["name"] == "Kath" and p["aliases"] == ["Kat"] and p["has_voice"]
    # renaming back keeps the id too; the new name stops being an alias
    call(mcp, "rename_speaker_everywhere", old_name="Kath", new_name="Kat")
    (p,) = [p for p in people_json(pipeline) if p["id"] == kat]
    assert p["name"] == "Kat" and p["aliases"] == ["Kath"]
    # nothing to rename: nobody's id or name changes
    assert "No lines by 'Kath'" in call(mcp, "rename_speaker_everywhere", old_name="Kath",
                                        new_name="X")
    assert store.person_id("X") is None and person(store, kat)["name"] == "Kat"


def test_rename_onto_an_existing_person_merges_into_that_id(pipeline):
    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    process_two_speakers(pipeline)
    conv = store.list_conversations()[0]["id"]
    say(store, "other", T0 + 5 * HOUR, "Katherine", "Katherine plays chess",
        people=("Katherine",))
    katherine = store.person_id("Katherine")  # mentioned only, no voice
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Kat")
    kat = store.person_id("Kat")
    call(mcp, "rename_speaker_everywhere", old_name="Kat", new_name="Katie")  # alias "Kat"
    assert store.person_id("Katie") == kat

    out = call(mcp, "rename_speaker_everywhere", old_name="Katie", new_name="Katherine")
    assert f"(person id {kat}) is merged into Katherine, whose person id {katherine} is kept" \
        in out
    # the name that already existed keeps its id; old names and aliases resolve to it
    assert store.person_id("katie") == store.person_id("Kat") == katherine
    assert person(store, kat)["merged_into"] == katherine
    survivor = person(store, katherine)
    assert survivor["speaker_id"] is not None  # the voice moved with the merge
    people = people_json(pipeline)
    assert kat not in [p["id"] for p in people]  # merged rows are not listed
    (p,) = [p for p in people if p["id"] == katherine]
    assert p["aliases"] == ["Kat", "Katie"] and p["has_voice"]
    assert p["conversations"] == 2  # spoke in one, was mentioned in the other

    # renaming the survivor to a merged name keeps it whole (no name clash)
    call(mcp, "rename_speaker_everywhere", old_name="Katherine", new_name="Kat")
    assert store.person_id("Kat") == store.person_id("Katherine") == katherine
    assert person(store, katherine)["name"] == "Kat"


def test_owner_moves_with_a_merge(pipeline):
    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    process_two_speakers(pipeline)
    say(store, "other", T0 + 5 * HOUR, "Sam", "Sam likes tea", people=("Sam",))
    sam = store.person_id("Sam")
    call(mcp, "rename_speaker_everywhere", old_name="Me", new_name="Sam")
    assert store.owner_person_id() == sam == store.person_id("me")


def test_list_people_json_shape_and_order(pipeline):
    store = pipeline.store
    process_two_speakers(pipeline)  # Me and an unnamed Speaker 1; summary names Alice, Me
    store.put_speaker("Zoe", voice(1200), is_owner=False)  # enrolled, never heard
    people = people_json(pipeline)
    me, alice, zoe = store.person_id("Me"), store.person_id("Alice"), store.person_id("Zoe")
    assert people == [
        {"id": me, "name": "Me", "aliases": [], "is_owner": True, "has_voice": True,
         "conversations": 1, "first_seen": "2025-10-09", "last_seen": "2025-10-09"},
        {"id": alice, "name": "Alice", "aliases": [], "is_owner": False,
         "has_voice": False, "conversations": 1, "first_seen": "2025-10-09",
         "last_seen": "2025-10-09"},
        {"id": zoe, "name": "Zoe", "aliases": [], "is_owner": False, "has_voice": True,
         "conversations": 0, "first_seen": None, "last_seen": None}]
    # the same order as the text mode: speakers first, then people mentioned
    text = call(build_mcp(store, pipeline.settings), "list_people")
    assert text.index("- Me") < text.index("Mentioned:") < text.index("- Alice")
    assert call(build_mcp(store, pipeline.settings), "list_people",
                format="xml") == "format must be text or json"


def test_list_people_text_is_unchanged(pipeline):
    store = pipeline.store
    process_two_speakers(pipeline)
    out = call(build_mcp(store, pipeline.settings), "list_people")
    assert out == "\n".join([
        "Speakers:",
        "- Speaker 1: 1 lines in 1 conversations, last 2025-10-09 08:53",
        "- Me (owner) (voice known): 1 lines in 1 conversations, last 2025-10-09 08:53",
        "Mentioned:",
        "- Me: 1 conversations, last 2025-10-09",
        "- Alice: 1 conversations, last 2025-10-09"])


def test_list_facts_json_person_id(pipeline):
    store = pipeline.store
    store.put_speaker("Dana", voice(800), is_owner=False)
    facts = {"Me": "Me prefers tea", "the user": "The user works nights",
             "alice": "Alice is vegetarian", "Dana": "Dana has two cats",
             "the Lisbon trip": "The Lisbon trip is in May"}
    for i, (subject, fact) in enumerate(facts.items()):
        say(store, f"c{i}", T0 + i * HOUR, subject, fact, people=("Alice",))
    data = json.loads(call(build_mcp(store, pipeline.settings), "list_facts", format="json"))
    got = {f["fact"]: (f["about"], f["person_id"]) for f in data["facts"]}
    me = store.owner_person_id()
    assert me == store.person_id("Me") is not None
    assert got == {
        "Me prefers tea": ("owner", me), "The user works nights": ("owner", me),
        "Alice is vegetarian": ("person", store.person_id("Alice")),
        "Dana has two cats": ("person", store.person_id("Dana")),
        "The Lisbon trip is in May": ("other", None)}


def test_fact_about_an_old_name_links_to_the_renamed_person(pipeline):
    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    process_two_speakers(pipeline)
    conv = store.list_conversations()[0]["id"]
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Kat")
    kat = store.person_id("Kat")
    call(mcp, "rename_speaker_everywhere", old_name="Kat", new_name="Kath")
    say(store, "later", T0 + 5 * HOUR, "Kat", "Kat runs marathons")
    data = json.loads(call(mcp, "list_facts", subject="Kat", format="json"))
    (fact,) = [f for f in data["facts"] if f["fact"] == "Kat runs marathons"]
    assert fact["about"] == "person" and fact["person_id"] == kat


def test_owner_name_setting_maps_to_the_owner_person(pipeline):
    store = pipeline.store
    settings = pipeline.settings.model_copy(update={"owner_name": "Sam"})
    say(store, "c0", T0, "sam", "Sam is allergic to peanuts")
    (fact,) = json.loads(call(build_mcp(store, settings), "list_facts",
                              format="json"))["facts"]
    assert fact["about"] == "owner" and fact["person_id"] == store.person_id("Me")


def owner_view(store: Store, settings) -> tuple[list[dict], list[dict]]:
    mcp = build_mcp(store, settings)
    people = json.loads(call(mcp, "list_people", format="json"))["people"]
    facts = json.loads(call(mcp, "list_facts", format="json"))["facts"]
    return people, facts


def test_without_an_enrolled_owner_the_owner_name_person_is_the_owner(pipeline, tmp_path):
    from convo_server.mcp_server import _owner_names

    store = Store(tmp_path / "no-owner.db")  # nobody enrolled as the owner
    say(store, "c0", T0, "Me", "Me prefers tea", people=("Me", "Alice"))
    say(store, "c1", T0 + HOUR, "Alice", "Alice is vegetarian", people=("Alice",))
    people, facts = owner_view(store, pipeline.settings)
    me = store.person_id("Me")
    assert [(p["name"], p["is_owner"]) for p in people] == [("Alice", False), ("Me", True)]
    assert store.owner_person_id(_owner_names(pipeline.settings)) == me
    assert {f["fact"]: f["person_id"] for f in facts} == {
        "Me prefers tea": me, "Alice is vegetarian": store.person_id("Alice")}


def test_owner_name_person_is_listed_as_the_enrolled_owner(pipeline, tmp_path):
    from convo_server.mcp_server import _owner_names

    store = Store(tmp_path / "sam.db")
    store.put_speaker("Sam", voice(300), is_owner=True)
    say(store, "c0", T0, "Me", "Me prefers tea", people=("Me", "Alice"))
    say(store, "c1", T0 + HOUR, "i", "I work nights", people=("Alice",))
    people, facts = owner_view(store, pipeline.settings)
    sam = store.person_id("Sam")
    owners = [p for p in people if p["is_owner"]]
    assert [p["id"] for p in owners] == [sam]  # exactly one owner row
    assert "Me" not in [p["name"] for p in people]  # no separate "Me" person
    assert owners[0]["aliases"] == ["Me"] and owners[0]["conversations"] == 1
    assert {f["person_id"] for f in facts if f["about"] == "owner"} == {sam}
    names = _owner_names(pipeline.settings)
    assert store.person_id("ME", names) == store.owner_person_id(names) == sam
    assert store.person_id("Me") != sam  # the row itself is kept, only mapped
