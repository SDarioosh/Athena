"""Characterization of the input checks on uploads and written notes.

Pins the error each bad input gets from ``Pipeline.ingest_chunk`` and
``Pipeline.ingest_text`` (the REST API answers 400 with this text), which check wins when
several are wrong, and what the helper checks return for good input. Written when the
checks moved out of ``pipeline.py``; the rows are what the code returned before the move.
"""

from __future__ import annotations

from typing import Any

import pytest

from athena.pipeline import Pipeline, validate_link, validate_track, written_lines
from tests.conftest import silence, wav

ID = "must match ^[A-Za-z0-9._:-]{1,128}$"
TRACK_RE = "track must match ^[A-Za-z0-9._:-]{1,64}$"
KIND = "kind must be one of ['conversation', 'note']"
CODEC = "codec must be one of ['aac', 'm4a', 'mp3', 'ogg', 'opus', 'pcm16', 'wav', 'webm']"
WRITTEN = ("device 'handwriting' and session ids starting with 'written:' are for written "
           "notes (POST /v1/ingest/text)")
SOURCE = ("source_id must look like hub-note:<id> (id: letters, digits, '.', '_' or '-', "
          "at most 100)")
LINK = ("link must be an http(s) URL of at most 2000 characters, without spaces, quotes, "
        "( ) < > [ ] { } | \\ ^ `")
WHEN = "written_at must be a date between 2000 and 2100"
TITLE = "title is longer than 200 characters"
NO_WORDS = "text has no words; to remove a note use DELETE /v1/ingest/text/{source_id}"

CHUNK_CASES: list[tuple[dict[str, Any], str]] = [
    ({}, "ok"),
    ({"device": "bad device"}, f"device {ID}"),
    ({"device": ""}, f"device {ID}"),
    ({"session_id": "s/1"}, f"session_id {ID}"),
    ({"session_id": "x" * 129}, f"session_id {ID}"),
    ({"track": "bad track!"}, TRACK_RE),
    ({"track": "t" * 65}, TRACK_RE),
    ({"track_name": "Dana"}, "track_name needs a track"),
    ({"track": "t1", "track_name": "x" * 201}, "track_name is longer than 200 characters"),
    ({"track": "t1", "track_name": "\x00\x01"}, "ok"),
    ({"device": "handwriting"}, WRITTEN),
    ({"session_id": "hub-note:abc"}, "ok"),
    ({"session_id": "written-1"}, "ok"),
    ({"session_id": "written:1"}, WRITTEN),
    ({"sequence": -1}, "sequence must be >= 0"),
    ({"kind": "bogus"}, KIND),
    ({"kind": " NOTE "}, "ok"),
    ({"kind": ""}, "ok"),
    ({"codec": "mp3x"}, CODEC),
    ({"codec": "WAV"}, "ok"),
    ({"data": b""}, "empty audio"),
    # several wrong: the first check in this order wins
    ({"device": "bad device", "sequence": -1, "codec": "zz"}, f"device {ID}"),
    ({"session_id": "s/1", "track": "bad!"}, f"session_id {ID}"),
    ({"track": "bad!", "device": "handwriting"}, TRACK_RE),
    ({"device": "handwriting", "sequence": -1}, WRITTEN),
    ({"sequence": -1, "kind": "bogus"}, "sequence must be >= 0"),
    ({"kind": "bogus", "codec": "zz"}, KIND),
    ({"codec": "zz", "data": b""}, CODEC),
]

TEXT_CASES: list[tuple[dict[str, Any], str]] = [
    ({}, "ok: created"),
    ({"source_id": "nope"}, SOURCE),
    ({"source_id": "hub-note:"}, SOURCE),
    ({"source_id": "hub-note:" + "a" * 101}, SOURCE),
    ({"link": "ftp://x"}, LINK),
    ({"link": "https://hub/(x)"}, LINK),
    ({"link": "https://h/" + "a" * 2000}, LINK),
    ({"link": " https://hub.example/#n=1 "}, "ok: created"),
    ({"written_at": 946684799.0}, WHEN),
    ({"written_at": 946684800.0}, "ok: created"),
    ({"written_at": 4102444800.0}, WHEN),
    ({"title": "x" * 201}, TITLE),
    ({"title": "\x00 a \n b "}, "ok: created"),
    ({"text": ""}, NO_WORDS),
    ({"text": " \n\x01\n "}, NO_WORDS),
    ({"text": "x" * 200_001}, "text is longer than 200000 characters"),
    ({"text": "a\n" * 5001}, "text has more than 5000 lines"),
    # several wrong: the first check in this order wins
    ({"source_id": "nope", "link": "ftp://x"}, SOURCE),
    ({"link": "ftp://x", "written_at": 0.0}, LINK),
    ({"written_at": 0.0, "title": "x" * 201}, WHEN),
    ({"title": "x" * 201, "text": ""}, TITLE),
]


@pytest.mark.parametrize("over, want", CHUNK_CASES)
def test_chunk_checks(pipeline: Pipeline, over: dict[str, Any], want: str) -> None:
    args: dict[str, Any] = dict(device="watch", session_id="s1", sequence=0,
                                started_at=1792317600.0, codec="wav", data=wav(silence(1)))
    try:
        pipeline.ingest_chunk(**{**args, **over})
        got = "ok"
    except ValueError as e:
        got = str(e)
    assert got == want


@pytest.mark.parametrize("over, want", TEXT_CASES)
def test_text_checks(pipeline: Pipeline, over: dict[str, Any], want: str) -> None:
    args: dict[str, Any] = dict(source_id="hub-note:n1", text="Buy milk",
                                written_at=1792317600.0,
                                link="https://hub.example/#open=notes&note=n1")
    try:
        got = "ok: " + pipeline.ingest_text(**{**args, **over})["result"]
    except ValueError as e:
        got = str(e)
    assert got == want


def test_delete_text_checks_the_id(pipeline: Pipeline) -> None:
    with pytest.raises(ValueError, match="source_id must look like"):
        pipeline.delete_text("nope")
    assert pipeline.delete_text("hub-note:missing") is None


def test_stored_title_is_cleaned(pipeline: Pipeline) -> None:
    out = pipeline.ingest_text(source_id="hub-note:n2", text="Buy milk",
                               written_at=1792317600.0, title="\x00 Shopping \n list ",
                               link="https://hub.example/#n=2")
    conv = pipeline.store.get_conversation(out["conversation_id"])
    assert conv is not None and conv["written"]["title"] == "Shopping list"


@pytest.mark.parametrize("given, want", [
    ((None, None), ("", None)), (("", None), ("", None)), (("t1", None), ("t1", None)),
    (("t1", "  Dana \t Example\x07 "), ("t1", "Dana Example")), (("t1", "\x00"), ("t1", None)),
    (("", "  "), ("", None))])
def test_track_values(given: tuple[str | None, str | None], want: tuple[str, str | None]) -> None:
    assert validate_track(*given) == want


@pytest.mark.parametrize("text, want", [
    ("a\nb", ["a", "b"]), ("  a   b \r\n\n c\x0b", ["a b", "c"]), ("a\tb", ["a b"]),
    ("x" * 4001, [4000, 1]), (("word " * 900).strip(), [3999, 499])])
def test_written_lines(text: str, want: list[Any]) -> None:
    got = written_lines(text)
    assert (got if isinstance(want[0], str) else [len(x) for x in got]) == want


@pytest.mark.parametrize("link, want", [
    (" https://hub.example/#open=notes&note=n1 ", "https://hub.example/#open=notes&note=n1"),
    ("http://[::1]:7010/x", "http://[::1]:7010/x"),
    ("https://h/[x]", None), ("https://h/a b", None), ("https://user@h/", None)])
def test_links(link: str, want: str | None) -> None:
    if want is None:
        with pytest.raises(ValueError, match="link must be"):
            validate_link(link)
    else:
        assert validate_link(link) == want
