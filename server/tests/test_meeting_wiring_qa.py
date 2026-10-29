"""Cross-lane QA for the Zoom meeting wiring (lanes 2A + 2B): one room rule for both
lanes, rooms and conference hardware end to end (BUG-01), the owner's one identity in a
meeting (BUG-07, BUG-08). Fictional names only."""

from __future__ import annotations

import re

import pytest
from conftest import tone
from test_meeting_tracks import SCRIPT, T0, ScriptedWhisper, track_audio

from athena.meeting_names import is_room_or_hardware
from athena.meeting_tracks import is_shared_room

_SPEAKER_N = re.compile(r"^Speaker \d+$")

# ----------------------------------------------------- one room rule for both lanes

ROOMS = [
    # rooms and conference hardware
    "Conference Room", "Boardroom", "Meeting Room 2", "Zoom Rooms", "ZoomRoom-3F",
    "Polycom", "Logitech Rally", "Rally Bar", "Huddle 4", "Conf Rm A", "Rm 204", "Lobby",
    "Main Hall", "Hall 2", "Cisco Webex Room", "Meetup", "Speakerphone", "SM-G991B",
    "Kath Dlamini | Boardroom",
    # a bare device with no person on it: who holds it is unknown, so the diarizer decides
    "iPhone", "Galaxy S23", "Pixel 8", "MacBook Pro",
    # a dial-in line (it can carry a room) and generic names
    "+1 555 0100", "Dial-in user", "Zoom user",
]

PEOPLE = [
    "Jordan Hall", "Annie Hall", "Hall", "Cisco Ramirez", "Kath's iPhone",
    "Alice Example", "Bo Example (he/him)", "Roomi Example", "Hallie Example",
    "Max Pixelton", "Mac Example", "Mac", "Dell", "Owen Polyakov",
    "Dr. Cleo Example | Acme", "", None,
]


@pytest.mark.parametrize("name", ROOMS)
def test_rooms_and_hardware_are_shared_in_both_lanes(name):
    assert is_room_or_hardware(name)
    assert is_shared_room(name)


@pytest.mark.parametrize("name", PEOPLE)
def test_people_are_not_rooms_in_either_lane(name):
    """2A matched "hall" and "cisco" anywhere ("Jordan Hall", "Cisco Ramirez") and took a
    personal phone ("Kath's iPhone") for a shared room; 2B names those people."""
    assert not is_room_or_hardware(name)
    assert not is_shared_room(name)


# ---------------------------------------------- BUG-01 end to end: rooms, hardware

BUG01_NAMES = ["Polycom", "Logitech Rally", "Huddle 4", "Conf Rm A", "ZoomRoom-3F",
               "SM-G991B", "Lobby", "Speakerphone"]


def _meeting(pipeline, tracks, owners=""):
    """tracks: (track id, display name, started_at, wav bytes)."""
    pipeline.transcriber = ScriptedWhisper()
    pipeline.settings.zoom_owner_names = owners
    for track, name, start, data in tracks:
        pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=0, started_at=start,
                              codec="wav", data=data, track=track, track_name=name)
    pipeline.end_session("m1")
    pipeline.process_session("m1")
    return pipeline.store.session_segments("m1")


def test_bug01_room_and_hardware_tracks_are_diarized_and_name_no_one(pipeline):
    """QA BUG-01 reproducer: each track holds two voices; every one is diarized into
    "Speaker N", and no person, alias or line is named after the room or device."""
    calls = []
    real = pipeline.diarizer.diarize_runs
    pipeline.diarizer.diarize_runs = lambda *a: calls.append(1) or real(*a)
    two_voices = track_audio((1, tone(900, 4)), (9, tone(1100, 4)))
    # a track every 30 s so the same scripted words never look like bleed
    segs = _meeting(pipeline, [(f"t{i}", n, T0 + 30 * i, two_voices)
                               for i, n in enumerate(BUG01_NAMES)])
    assert len(calls) == len(BUG01_NAMES)
    assert len(segs) == 2 * len(BUG01_NAMES)
    assert all(_SPEAKER_N.match(s.speaker) and not s.is_owner for s in segs), [
        s.speaker for s in segs]
    assert len({s.speaker for s in segs}) == 2 * len(BUG01_NAMES)
    assert {s.text for s in segs} == {SCRIPT[900], SCRIPT[1100]}
    devices = {n.casefold() for n in BUG01_NAMES}
    for p in pipeline.store.people():  # "Me" and the fake summary's people only
        assert p["name"].casefold() not in devices, p
        assert not any(a.casefold() in devices for a in p["aliases"]), p


def test_a_personal_phone_is_its_person_not_a_room(pipeline):
    calls = []
    real = pipeline.diarizer.diarize_runs
    pipeline.diarizer.diarize_runs = lambda *a: calls.append(1) or real(*a)
    segs = _meeting(pipeline, [("t1", "Kath's iPhone", T0,
                                track_audio((1, tone(900, 4))))])
    assert calls == []
    assert [(s.speaker, s.is_owner) for s in segs] == [("Kath", False)]


def test_a_person_named_hall_is_not_diarized(pipeline):
    pipeline.diarizer.diarize_runs = lambda *a: pytest.fail("diarized a person")
    segs = _meeting(pipeline, [("t1", "Jordan Hall", T0, track_audio((1, tone(900, 4))))])
    assert [s.speaker for s in segs] == ["Jordan Hall"]


# ---------------------------------------- BUG-07 / BUG-08 end to end: the owner


def _no_owner_voice(pipeline):
    pipeline.store._conn.execute("DELETE FROM speakers")
    pipeline.store._conn.execute("DELETE FROM people")
    pipeline.diarizer.voices = []
    assert pipeline.store.owner_name() is None


def test_bug07_owner_lines_and_owner_person_share_one_name(pipeline):
    """QA BUG-07: owner matched by CONVO_ZOOM_OWNER_NAMES, no owner voice enrolled. The
    owner's lines and the owner's person carry one name, not "Me" next to "Cleo"; that
    name is CONVO_OWNER_NAME and the Zoom names are aliases (owner request)."""
    _no_owner_voice(pipeline)
    pipeline.settings.owner_name = "Shahyah"
    segs = _meeting(pipeline, [("t1", "Cleo Example (she/her)", T0,
                                track_audio((1, tone(700, 4)))),
                               ("t2", "Bo Example", T0, track_audio((9, tone(500, 4))))],
                    owners="Cleo Example")
    assert [(s.speaker, s.is_owner) for s in segs] == [("Shahyah", True),
                                                       ("Bo Example", False)]
    owners = [p for p in pipeline.store.people() if p["is_owner"]]
    assert [p["name"] for p in owners] == ["Shahyah"]
    assert {"Cleo Example", "Cleo Example (she/her)"} <= set(owners[0]["aliases"])
    assert pipeline.store.owner_name() is None


def test_owner_request_voice_note_after_a_zoom_meeting_keeps_convo_owner_name(pipeline):
    """Owner request: after a meeting matched the owner by Zoom name (no owner voice), a
    voice note is still written under CONVO_OWNER_NAME, the same person as the meeting."""
    _no_owner_voice(pipeline)
    pipeline.settings.owner_name = "Shahyah"
    _meeting(pipeline, [("t1", "Cleo Example", T0, track_audio((1, tone(700, 4))))],
             owners="Cleo Example")
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="twatch", session_id="n1", sequence=0, started_at=T0 + 3600,
                          codec="wav", data=track_audio((1, tone(700, 4))), kind="note")
    pipeline.end_session("n1")
    pipeline.process_session("n1")
    assert {(s.speaker, s.is_owner) for s in pipeline.store.session_segments("n1")} == {
        ("Shahyah", True)}
    owners = [p for p in pipeline.store.people() if p["is_owner"]]
    assert [p["name"] for p in owners] == ["Shahyah"] and owners[0]["conversations"] == 2


def test_owner_name_is_also_an_owner_zoom_name(pipeline):
    """Owner request: CONVO_OWNER_NAME="Shahyah Darioosh" and the same Zoom name, with
    CONVO_ZOOM_OWNER_NAMES unset: that track is the owner's, one person, no alias twice."""
    _no_owner_voice(pipeline)
    pipeline.settings.owner_name = "Shahyah Darioosh"
    segs = _meeting(pipeline, [("t1", "Shahyah Darioosh", T0, track_audio((1, tone(700, 4)))),
                               ("t2", "Bo Example", T0, track_audio((9, tone(500, 4))))])
    assert [(s.speaker, s.is_owner) for s in segs] == [("Shahyah Darioosh", True),
                                                       ("Bo Example", False)]
    owners = [p for p in pipeline.store.people() if p["is_owner"]]
    assert [p["name"] for p in owners] == ["Shahyah Darioosh"]
    assert "Shahyah Darioosh" not in owners[0]["aliases"]
    assert len({a.lower() for a in owners[0]["aliases"]}) == len(owners[0]["aliases"])


def test_default_owner_name_me_is_not_an_owner_zoom_name(pipeline):
    """The default CONVO_OWNER_NAME "Me" is a placeholder: a participant shown as "Me" is
    not the owner unless CONVO_ZOOM_OWNER_NAMES lists it."""
    assert pipeline.settings.owner_name == "Me"
    assert pipeline.settings.zoom_owner_name_list == ()
    segs = _meeting(pipeline, [("t1", "Me", T0, track_audio((1, tone(700, 4))))])
    assert not any(s.is_owner for s in segs)


def test_listed_owner_zoom_names_still_work_besides_owner_name(pipeline):
    _no_owner_voice(pipeline)
    pipeline.settings.owner_name = "Shahyah Darioosh"
    pipeline.settings.zoom_owner_names = "Shahyah (phone), shahyah darioosh"
    assert pipeline.settings.zoom_owner_name_list == ("Shahyah (phone)", "shahyah darioosh")
    pipeline.settings.zoom_owner_names = "Shahyah (phone)"
    assert pipeline.settings.zoom_owner_name_list == ("Shahyah (phone)", "Shahyah Darioosh")
    segs = _meeting(pipeline, [("t1", "Shahyah (phone)", T0, track_audio((1, tone(700, 4))))],
                    owners="Shahyah (phone)")
    assert [(s.speaker, s.is_owner) for s in segs] == [("Shahyah Darioosh", True)]
    owners = [p for p in pipeline.store.people() if p["is_owner"]]
    assert [p["name"] for p in owners] == ["Shahyah Darioosh"]


@pytest.mark.parametrize("raw", ["Me", "iPhone"])
def test_bug08_owner_joining_as_a_bare_name_is_the_owner(pipeline, raw):
    """QA BUG-08: the owner joins as "Me" or "iPhone", both listed in owner names: owner
    lines (is_owner), never diarized as a room, no "Speaker N", no person of that name."""
    pipeline.diarizer.diarize_runs = lambda *a: pytest.fail("owner track diarized")
    segs = _meeting(pipeline, [("t1", raw, T0, track_audio((1, tone(700, 4))))],
                    owners="Me, iPhone")
    assert [(s.speaker, s.is_owner) for s in segs] == [("Me", True)]
    people = pipeline.store.people()
    assert [p["name"] for p in people if p["is_owner"]] == ["Me"]
    assert not any(p["name"] == "iPhone" or "iPhone" in p["aliases"] for p in people)


def test_bug08_without_an_owner_voice(pipeline):
    _no_owner_voice(pipeline)
    pipeline.settings.owner_name = "Sam"
    pipeline.diarizer.diarize_runs = lambda *a: pytest.fail("owner track diarized")
    segs = _meeting(pipeline, [("t1", "iPhone", T0, track_audio((1, tone(700, 4))))],
                    owners="iPhone")
    assert [s.is_owner for s in segs] == [True]
    assert not any(_SPEAKER_N.match(s.speaker) for s in segs)
    owners = [p["name"] for p in pipeline.store.people() if p["is_owner"]]
    assert {s.speaker for s in segs} == set(owners or ["Sam"])
