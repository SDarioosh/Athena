"""athena.validation: the input checks, apart from the pipeline (the table of errors is
test_input_checks.py)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from athena import pipeline, validation


def test_meeting_chat_does_not_import_the_pipeline() -> None:
    """The meeting chat module took its checks from pipeline.py, which imports it back
    (summary_context): a cycle. Its checks now come from validation.py."""
    code = ("import sys, athena.meetings.ingest.chat, athena.validation, athena.timeutil; "
            "print('athena.pipeline' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("name", [
    "MAX_LINE_CHARS", "MAX_LINK_CHARS", "MAX_TEXT_CHARS", "MAX_TEXT_LINES", "MAX_TITLE_CHARS",
    "MAX_TRACK_NAME_CHARS", "SOURCE_ID", "TRACK", "validate_id", "validate_link",
    "validate_source_id", "validate_track", "written_lines"])
def test_old_names_still_read_from_pipeline(name: str) -> None:
    assert getattr(pipeline, name) is getattr(validation, name)


def test_check_chunk_returns_stored_values() -> None:
    assert validation.check_chunk(
        device="watch", session_id="s1", sequence=0, kind=" Note ", codec="OPUS",
        data=b"x", track="t1", track_name=" Dana\x00  Example ") == (
        "note", "opus", "t1", "Dana Example")
    assert validation.check_chunk(
        device="watch", session_id="s1", sequence=3, kind=None, codec="wav", data=b"x",
        track=None, track_name=None) == ("conversation", "wav", "", None)


def test_check_written_note_returns_stored_values() -> None:
    assert validation.check_written_note(
        source_id="hub-note:n1", link=" https://hub.example/#n=1 ", written_at=1792317600.0,
        title=" A\x00  title ") == ("https://hub.example/#n=1", "A title")
    assert validation.check_written_note(
        source_id="hub-note:n1", link="https://hub.example/", written_at=1792317600.0,
        title="  ") == ("https://hub.example/", None)
