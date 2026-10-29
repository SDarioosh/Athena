"""The meeting modules moved into ``athena.meetings``; the old import paths still work."""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

MOVED = {
    "athena.meeting_chat": "athena.meetings.ingest.chat",
    "athena.meeting_live": "athena.meetings.transcribe.live",
    "athena.meeting_names": "athena.meetings.people.names",
    "athena.meeting_tracks": "athena.meetings.transcribe.tracks",
    "athena.meeting_voices": "athena.meetings.people.voices",
    "athena.zoom_signin": "athena.meetings.bot.zoom_signin",
}


@pytest.mark.parametrize("old,new", sorted(MOVED.items()))
def test_old_path_is_the_moved_module(old: str, new: str) -> None:
    module = importlib.import_module(new)
    assert importlib.import_module(old) is module
    assert sys.modules[old] is module
    alias = "convo_server." + old.split(".", 1)[1]
    assert importlib.import_module(alias) is module


def test_monkeypatching_the_old_path_reaches_the_real_module(monkeypatch) -> None:
    from athena import meeting_tracks
    from athena.meetings.transcribe import tracks

    monkeypatch.setattr(meeting_tracks, "BLEED_MIN_DB", 99.0)
    assert tracks.BLEED_MIN_DB == 99.0
    monkeypatch.setattr("athena.meeting_names.PLACEHOLDER", "Nobody")
    from athena.meetings.people import names
    assert names.PLACEHOLDER == "Nobody"


def test_meetings_package_serves_the_launcher_names() -> None:
    import athena.meetings as package
    from athena.meetings.bot import launcher

    names = [n for n in vars(launcher) if not n.startswith("__")]
    assert len(names) > 30
    for name in names:
        assert getattr(package, name) is getattr(launcher, name), name
    import convo_server.meetings

    from athena.meetings import Launcher, MeetingError, parse_zoom_link  # noqa: F401
    assert convo_server.meetings.Launcher is launcher.Launcher
    with pytest.raises(AttributeError):
        package.no_such_name  # noqa: B018
    with pytest.raises(ImportError):
        from athena.meetings import no_such_name  # noqa: F401


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
        import athena.meeting_names, athena.meeting_tracks, athena.meeting_live
        import athena.meeting_voices, athena.meeting_chat
        from athena.meetings import people, transcribe  # noqa: F401
        bad = [m for m in sys.modules if m.startswith("athena.meetings.bot")]
        assert not bad, bad
        from athena.meetings import Launcher  # noqa: F401
        assert "athena.meetings.bot.launcher" in sys.modules
        assert "athena.meetings.bot.zoom_signin" not in sys.modules
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parents[1], timeout=60)
    assert r.stdout.strip() == "ok", r.stderr
