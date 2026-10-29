"""The meeting modules in ``athena.meetings``: their logger names and what importing
them loads."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path


def test_moved_modules_keep_their_logger_names() -> None:
    from athena.meetings.bot import launcher
    from athena.meetings.people import voices
    from athena.meetings.transcribe import live, tracks

    assert [m.log.name for m in (launcher, live, tracks, voices)] == [
        "athena.meetings", "athena.meeting_live", "athena.meeting_tracks",
        "athena.meeting_voices"]
    assert all(isinstance(m.log, logging.Logger) for m in (launcher, live, tracks, voices))


def test_meeting_subpackages_never_load_the_launcher() -> None:
    """Names, voices, live transcription and chat run during a meeting's processing, not
    on a meeting request: importing them loads no bot launcher or Zoom sign-in (a fresh
    interpreter, so other tests can't pollute sys.modules)."""
    code = textwrap.dedent("""
        import sys
        import athena.meetings.people.names, athena.meetings.transcribe.tracks
        import athena.meetings.transcribe.live, athena.meetings.people.voices
        import athena.meetings.ingest.chat
        from athena.meetings import people, transcribe  # noqa: F401
        bad = [m for m in sys.modules if m.startswith("athena.meetings.bot")]
        assert not bad, bad
        from athena.meetings.bot.launcher import Launcher  # noqa: F401
        assert "athena.meetings.bot.launcher" in sys.modules
        assert "athena.meetings.bot.zoom_signin" not in sys.modules
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parents[2], timeout=60)
    assert r.stdout.strip() == "ok", r.stderr
