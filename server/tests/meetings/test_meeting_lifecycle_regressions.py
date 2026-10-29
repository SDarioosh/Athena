"""Regressions in the meeting bot's lifecycle (launcher, relay). The bot is
tests/meetings/fake_meeting_bot.py; meeting numbers and keys are fictional."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from athena.meetings.bot import launcher as meetings
from athena.meetings.bot.launcher import Launcher, _Running, final_state
from tests.meetings.test_meetings import (  # noqa: F401  (meeting_settings is a fixture)
    _launcher,
    _report,
    _use,
    _wait,
    meeting_settings,
)
from tests.meetings.test_relay_meetings import Q, fake


# --------------------------------------------------- relay: GET /meetings?limit
@pytest.mark.parametrize("raw", ["²", "9" * 5000])
def test_list_limit_odd_digits_are_not_a_500(pipeline, raw):
    """``"²".isdigit()`` is true but ``int("²")`` raises, and so does int() of a digit
    string over Python's 4300-digit limit: the relay answered 500."""
    c, up = fake(pipeline, 200, {"meetings": []})
    r = c.get(f"/meetings{Q}&limit={quote(raw)}")
    assert r.status_code in (200, 400), r.text
    if r.status_code == 200:
        assert dict(up.seen[0].url.params) == {"limit": "100"}
    else:
        assert "limit" in r.json()["detail"] and not up.seen


# ------------------------------------------- settings the bot refuses to start with
@pytest.mark.parametrize(("name", "value"), [
    ("meeting_alone_seconds", 0.5),
    ("meeting_waiting_room_seconds", 0),
    ("max_session_seconds", 1e10),
])
def test_durations_the_bot_refuses_are_a_501(meeting_settings, client, tmp_path,  # noqa: F811
                                             name, value):
    """The bot takes --max-seconds, --alone-seconds and --waiting-room-seconds as whole
    numbers from 1 to 999999999 (meeting-bot/bot/src/options.cpp) and otherwise exits
    with ``failed bad_arguments``. The join answered 202 and the meeting then failed
    with nothing saying which setting was wrong."""
    rep = _use(meeting_settings, tmp_path, "record")
    setattr(meeting_settings, name, value)
    r = client.post("/v1/meetings", json={"meeting_id": "123456789"})
    assert r.status_code == 501, r.text
    assert "SECONDS" in r.json()["detail"]
    assert not rep.exists() and _launcher(client).running == {}


# ------------------------------------------------ the bot's "stopped" vs our reason
def test_launcher_timeout_is_max_duration_not_stopped(meeting_settings, client,  # noqa: F811
                                                      tmp_path, monkeypatch):
    """The launcher's own lifetime timeout SIGTERMs the bot, which answers every SIGTERM
    with ``STATE ended stopped`` (rules.cpp stop_signal). That line won, so the hub said
    "Ended: you stopped it." for a meeting nobody stopped."""
    monkeypatch.setattr(meetings, "LIFETIME_GRACE_SECONDS", 0.0)
    meeting_settings.max_session_seconds = 1
    _use(meeting_settings, tmp_path, "record")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "ended", "failed")
    assert (row["state"], row["reason"]) == ("ended", "max_duration")


def test_final_state_keeps_the_bots_own_reasons():
    assert final_state(("ended", "alone"), 0, "stopped") == ("ended", "alone")
    assert final_state(("failed", "wrong_passcode"), 2, "stopped") == \
        ("failed", "wrong_passcode")
    assert final_state(("ended", "stopped"), 0, None) == ("ended", "stopped")
    assert final_state(("ended", "stopped"), 0, "stopped") == ("ended", "stopped")


# ------------------------------------------------ a stop that comes after the exit
def test_stop_after_the_bot_exited_keeps_its_crash(pipeline, meeting_settings):  # noqa: F811
    """A DELETE that arrives after the bot crashed, before the watcher wrote the final
    row, set the stop reason anyway: a crash read as ``ended stopped``."""
    async def go() -> tuple[str, str]:
        launcher = Launcher(pipeline)
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"],
                                stdout=subprocess.PIPE)
        code = proc.wait()
        exited = asyncio.get_running_loop().create_future()
        exited.set_result(code)
        run = _Running("0" * 32, "zoom-x", proc, "plain", "u", Path("/nonexistent"), exited)
        await launcher._terminate(run, "stopped")
        proc.stdout.close()
        return final_state(run.last, code, run.stop_reason)

    assert asyncio.run(go()) == ("failed", "bot_crashed")


# ------------------------------------------------- server shutdown as a container init
def test_shutdown_as_container_init_lets_bots_leave(meeting_settings, pipeline,  # noqa: F811
                                                    tmp_path, monkeypatch):
    """In the sound-api container convo-server is PID 1 (the entrypoint execs it); when
    PID 1 exits the kernel SIGKILLs every other process of the container, so a bot that
    "keeps running" over a shutdown loses its last chunks, the meeting's chat and the
    session's end. There the shutdown must ask the bots to leave (SIGTERM) first."""
    from fastapi.testclient import TestClient

    from athena.api import create_app

    monkeypatch.setattr(meetings, "_exit_kills_bots", lambda: True, raising=False)
    rep = _use(meeting_settings, tmp_path, "record")
    with TestClient(create_app(pipeline)) as c:
        c.headers["Authorization"] = "Bearer " + meeting_settings.ingest_token
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "recording")
        pid = _report(rep)["pid"]
    try:
        row = pipeline.store.get_meeting(mid)
        assert (row["state"], row["reason"]) == ("ended", "server_stop")
        assert not Path(f"/proc/{pid}").exists() or \
            "zombie" in Path(f"/proc/{pid}/status").read_text()
    finally:
        with __import__("contextlib").suppress(ProcessLookupError):
            __import__("os").kill(pid, 9)


def test_shutdown_elsewhere_leaves_bots_running(meeting_settings, pipeline,  # noqa: F811
                                                tmp_path, monkeypatch):
    """Not PID 1: unchanged, the bot keeps running and a later request reconciles it."""
    import os
    import signal

    monkeypatch.setattr(meetings, "_exit_kills_bots", lambda: False, raising=False)
    rep = _use(meeting_settings, tmp_path, "record")
    from fastapi.testclient import TestClient

    from athena.api import create_app

    with TestClient(create_app(pipeline)) as c:
        c.headers["Authorization"] = "Bearer " + meeting_settings.ingest_token
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "recording")
        pid = _report(rep)["pid"]
    try:
        assert pipeline.store.get_meeting(mid)["state"] == "recording"
        assert Path(f"/proc/{pid}").exists()
    finally:
        os.kill(pid, signal.SIGKILL)
        with __import__("contextlib").suppress(ChildProcessError):
            os.waitpid(pid, 0)
