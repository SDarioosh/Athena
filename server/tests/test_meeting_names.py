"""Zoom display names to people (Zoom bot plan, lane 2B). Fictional names only."""

from __future__ import annotations

import subprocess
import sys

import pytest

from convo_server.meeting_names import (
    clean_display_name,
    looks_like_device,
    speaker_for_track,
    speakers_for_tracks,
)
from convo_server.store import Segment, Store

OWNER = ("Sam Okoro",)


@pytest.mark.parametrize("raw, clean", [
    # pronouns and tags
    ("Kath Dlamini (she/her)", "Kath Dlamini"),
    ("Tomasz Nowak (he/him)", "Tomasz Nowak"),
    ("Rui Tanaka (they/them)", "Rui Tanaka"),
    ("Kath Dlamini she/her", "Kath Dlamini"),
    ("Kath Dlamini [she/they]", "Kath Dlamini"),
    ("Kath [Acme] (she/her)", "Kath"),
    ("Ana Ruiz (Guest)", "Ana Ruiz"),
    # company
    ("Kath Dlamini | Acme", "Kath Dlamini"),
    ("Kath Dlamini|Acme Ltd", "Kath Dlamini"),
    ("Ana - Acme Ltd", "Ana"),
    ("Ana – Acme", "Ana"),
    ("Ana @ Acme", "Ana"),
    ("Ana @Acme", "Ana"),
    ("Ana • Acme", "Ana"),
    ("Kath Dlamini (she/her) | Acme", "Kath Dlamini"),
    # hyphenated names are not "Name - Company"
    ("Ana Smith-Jones", "Ana Smith-Jones"),
    ("Jean-Luc Moreau", "Jean-Luc Moreau"),
    # devices
    ("Kath's iPhone", "Kath"),
    ("Kath’s MacBook Pro", "Kath"),
    ("James' Galaxy S21", "James"),
    ("Kath (iPad)", "Kath"),
    ("Kath iPhone", "Kath"),
    ("Kath iPhone 15 Pro", "Kath"),
    ("iPhone de Kath", "Kath"),
    ("KATH'S IPHONE", "Kath"),
    # emoji, punctuation, spacing, fancy letters
    ("🎉 Tomás 🎉", "Tomás"),
    ("Tomás ✨", "Tomás"),
    ("Priya 👋🏽 Raman", "Priya Raman"),
    ("  --Kath--  ", "Kath"),
    ("Kath    Dlamini", "Kath Dlamini"),
    ("𝓚𝓪𝓽𝓱", "Kath"),
    ("Ｋａｔｈ", "Kath"),
    ("(Kath)", "Kath"),
    # titles and credentials
    ("Dr. Ana Ruiz", "Ana Ruiz"),
    ("Ana Ruiz, PhD", "Ana Ruiz"),
    ("Prof Kath Dlamini", "Kath Dlamini"),
    # case: only ALL-CAPS and all-lowercase are fixed
    ("priya raman", "Priya Raman"),
    ("PRIYA RAMAN", "Priya Raman"),
    ("MARCUS MCDONALD", "Marcus McDonald"),
    ("Marcus McDonald", "Marcus McDonald"),
    ("Pieter van der Berg", "Pieter van der Berg"),
    ("PIETER VAN DER BERG", "Pieter van der Berg"),
    ("LIAM O'NEIL", "Liam O'Neil"),
    ("Liam O'Neil", "Liam O'Neil"),
    ("renée o'brien-mcarthur", "Renée O'Brien-McArthur"),
    ("JD OKAFOR", "JD Okafor"),
    ("deShawn Ellis", "deShawn Ellis"),
    ("José Ñúñez", "José Ñúñez"),
    ("Bo Li", "Bo Li"),
    ("Kath D.", "Kath D."),
    # user names and e-mail addresses
    ("kath.dlamini@acme.example", "Kath Dlamini"),
    ("kath_dlamini", "Kath Dlamini"),
])
def test_clean_display_name(raw, clean):
    assert clean_display_name(raw) == clean


@pytest.mark.parametrize("raw", [
    "iPhone", "iPad", "Galaxy S21", "Samsung Galaxy", "Android", "Pixel 7", "My iPhone",
    "DESKTOP-4KQ2B7", "LAPTOP-AB12", "Zoom user", "Zoom User 3", "Guest", "Host",
    "Conference Room 2", "Meeting Room", "Boardroom", "Room 4B", "Huddle Room (Acme)",
    "👍", "🎉🎉", "", "   ", "---", "Speaker 2", "Unknown", "+1 555 0100", "(she/her)",
])
def test_names_that_are_no_person_clean_to_empty(raw):
    assert clean_display_name(raw) == ""


def test_looks_like_device():
    assert looks_like_device("iPhone") and looks_like_device("Conference Room")
    assert looks_like_device("Zoom user") and looks_like_device("DESKTOP-4KQ2B7")
    assert not looks_like_device("Kath's iPhone")  # a person's device: the person
    assert not looks_like_device("Kath Dlamini")
    assert not looks_like_device("Galina Petrova")  # "Galaxy"-like, still a name


# ------------------------------------------------------------------ tracks to people
def track(*names: str, track_id: str = "16778240") -> dict:
    """A ``Store.tracks`` entry: ``names`` in first-use order, the last is the latest."""
    return {"track": track_id, "name": names[-1] if names else None, "names": list(names),
            "chunks": 1, "started_at": 0.0, "ended_at": 30.0}


def aliases(store: Store, pid: int) -> set[str]:
    return {r["alias"] for r in store._conn.execute(
        "SELECT alias FROM people_aliases WHERE person_id = ?", (pid,))}


def people_count(store: Store) -> int:
    return store._conn.execute(
        "SELECT COUNT(*) FROM people WHERE merged_into IS NULL").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "c.db")
    yield s
    s.close()


def test_unknown_name_becomes_a_new_person_with_zoom_name_as_alias(store):
    assert speaker_for_track(store, track("Kath Dlamini (she/her) | Acme"), OWNER) == (
        "Kath Dlamini", False)
    pid = store.person_id("Kath Dlamini")
    assert pid is not None
    assert "Kath Dlamini (she/her) | Acme" in aliases(store, pid)
    assert store.person_id("kath dlamini (she/her) | acme") == pid


def test_existing_person_matched_by_name_or_alias_any_case(store):
    kath = store.ensure_person("Kath Dlamini")
    assert speaker_for_track(store, track("KATH DLAMINI"), OWNER) == ("Kath Dlamini", False)
    store.link_person(["Priya Raman"], ["Pree"])
    priya = store.person_id("Priya Raman")
    assert speaker_for_track(store, track("pree 🎧"), OWNER) == ("Priya Raman", False)
    assert store.person_id("Kath Dlamini") == kath and people_count(store) == 2
    assert {"Pree", "pree 🎧"} <= aliases(store, priya)


def test_same_person_found_again_in_the_next_meeting(store):
    first = speaker_for_track(store, track("Kath's iPhone"), OWNER)
    pid = store.person_id("Kath")
    second = speaker_for_track(store, track("Kath (she/her)", track_id="99"), OWNER)
    assert first == second == ("Kath", False)
    assert store.person_id("Kath") == pid and people_count(store) == 1


def test_no_first_name_guessing(store):
    """Strict matching: a wrong merge is worse than a new person."""
    kath = store.ensure_person("Kath")
    assert speaker_for_track(store, track("Kath Dlamini"), OWNER) == ("Kath Dlamini", False)
    assert store.person_id("Kath Dlamini") != kath
    store.ensure_person("Priya Raman")
    assert speaker_for_track(store, track("Priya"), OWNER) == ("Priya", False)
    assert people_count(store) == 4


def test_rename_mid_meeting_stays_one_person(store):
    t = track("Kath's iPhone", "Kath Dlamini", "Kath D. (away)")
    assert speaker_for_track(store, t, OWNER) == ("Kath D.", False)
    pid = store.person_id("Kath D.")
    assert people_count(store) == 1
    assert {"Kath's iPhone", "Kath", "Kath Dlamini", "Kath D. (away)"} <= aliases(store, pid)
    # the next meeting under any of those names finds the same person
    assert speaker_for_track(store, track("Kath Dlamini", track_id="7"), OWNER)[0] == "Kath D."


def test_rename_matches_an_earlier_name_of_the_track(store):
    priya = store.ensure_person("Priya Raman")
    t = track("Priya Raman", "P. R. 🎧")  # renamed to something unknown
    assert speaker_for_track(store, t, OWNER) == ("Priya Raman", False)
    assert "P. R." in aliases(store, priya) and people_count(store) == 1


def test_owner_by_zoom_owner_names(store):
    assert speaker_for_track(store, track("SAM OKORO (he/him)"), OWNER) == ("Sam Okoro", True)
    owner = store.owner_person_id()
    assert owner == store.person_id("Sam Okoro")
    assert "SAM OKORO (he/him)" in aliases(store, owner)


def test_owner_keeps_the_enrolled_owner_name(store):
    import numpy as np
    store.put_speaker("Sam", np.ones(4, np.float32), is_owner=True)
    sam = store.person_id("Sam")
    assert speaker_for_track(store, track("Sam Okoro | Acme"), OWNER) == ("Sam", True)
    assert store.owner_person_id() == sam and "Sam Okoro" in aliases(store, sam)
    # a track named by an owner alias is the owner too
    assert speaker_for_track(store, track("sam okoro", track_id="5"), ()) == ("Sam", True)


def test_unusable_names_make_no_person(store):
    for t in (track("iPhone"), track("Zoom user"), track("👍"), track(),
              track("Conference Room 2")):
        assert speaker_for_track(store, t, OWNER) == ("Unknown", False)
    assert speaker_for_track(store, {**track("iPad"), "placeholder": "Speaker 3"},
                             OWNER) == ("Speaker 3", False)
    assert people_count(store) == 0
    assert store.person_id("iPhone") is None


def test_device_name_before_a_real_name_is_not_an_alias(store):
    speaker_for_track(store, track("iPhone", "Kath Dlamini"), OWNER)
    assert store.person_id("iPhone") is None
    assert "iPhone" not in aliases(store, store.person_id("Kath Dlamini"))


def test_speakers_for_tracks_numbers_unnamed_tracks(store):
    tracks = [track("Kath Dlamini", track_id="1"), track("iPhone", track_id="2"),
              track("Sam Okoro", track_id="3"), track("Zoom user", track_id="4")]
    assert speakers_for_tracks(store, tracks, OWNER) == {
        "1": ("Kath Dlamini", False), "2": ("Speaker 1", False),
        "3": ("Sam Okoro", True), "4": ("Speaker 2", False)}


def _line(store: Store, speaker: str, is_owner: bool, at: float) -> str:
    """One meeting line spoken by ``speaker``; returns its conversation id."""
    session = f"zoom-{at:.0f}"
    store.add_chunk(session_id=session, device="zoom", sequence=0, started_at=at,
                    duration=30, codec="opus", audio_path=None, speech=[])
    store.replace_segments(session, "zoom", [Segment(at, at + 2, "Hello there.", speaker,
                                                     is_owner)], [(at, at + 30)])
    return store.save_conversation(None, "zoom",
                                   [s.id for s in store.session_segments(session)])


def test_rename_speaker_everywhere_keeps_meeting_person_and_zoom_alias(store):
    name, owner = speaker_for_track(store, track("Kath Dlamini (she/her) | Acme"), OWNER)
    pid = store.person_id(name)
    conv = _line(store, name, owner, 100.0)
    n, convs = store.rename_everywhere("Kath Dlamini", "Kathleen Dlamini")
    assert n == 1 and convs == [conv]
    assert store.person_id("Kathleen Dlamini") == pid
    assert {"Kath Dlamini", "Kath Dlamini (she/her) | Acme"} <= aliases(store, pid)
    # the next meeting under the old Zoom name lands on the renamed person
    assert speaker_for_track(store, track("Kath Dlamini (she/her) | Acme", track_id="2"),
                             OWNER) == ("Kathleen Dlamini", False)


def test_rename_meeting_person_onto_existing_person_merges(store):
    kath = store.ensure_person("Kath")  # named in person earlier
    name, _ = speaker_for_track(store, track("Kath Dlamini (she/her)"), OWNER)
    zoom_pid = store.person_id(name)
    _line(store, name, False, 100.0)
    store.rename_everywhere("Kath Dlamini", "Kath")
    assert store.person_id("Kath Dlamini") == kath
    assert store.person_id("Kath Dlamini (she/her)") == kath
    merged = store._conn.execute("SELECT merged_into FROM people WHERE id = ?",
                                 (zoom_pid,)).fetchone()[0]
    assert merged == kath
    # from now on the Zoom name finds the merged person
    assert speaker_for_track(store, track("Kath Dlamini (she/her)", track_id="9"),
                             OWNER) == ("Kath", False)


def test_link_person_skips_placeholders_and_returns_current_name(store):
    assert store.link_person(["", "Speaker 1", "unknown"]) is None
    pid, name = store.link_person(["dana fox", "Dana Fox"], ["DF"])
    assert name == "dana fox" and store.person_id("df") == pid


def test_api_import_does_not_load_meeting_names():
    code = ("import sys, convo_server.api, convo_server.mcp_server; "
            "assert 'convo_server.meeting_names' not in sys.modules, 'loaded at startup'")
    subprocess.run([sys.executable, "-c", code], check=True)

