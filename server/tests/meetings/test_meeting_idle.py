"""Idle rule after a meeting (docs/zoom-meeting-bot-plan.md, "Resource rules"; lane 5C).

test_meetings.py checks that importing the API starts nothing and that a DELETE leaves no
watcher task. This checks the other end: after a bot leaves on its own, the server is
back where it was before the join. No new thread, child process, asyncio task, timer,
loop reader (the pidfd) or file descriptor, and no work dir. The bot is
tests/meetings/fake_meeting_bot.py; data is fictional.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import TOKEN

FAKE_BOT = Path(__file__).with_name("fake_meeting_bot.py")


def _children() -> set[int]:
    out: set[int] = set()
    for tid in os.listdir("/proc/self/task"):
        try:
            out |= {int(p) for p in Path(f"/proc/self/task/{tid}/children").read_text().split()}
        except OSError:
            continue
    return out


def _fds() -> dict[int, str]:
    out = {}
    for fd in os.listdir("/proc/self/fd"):
        try:
            out[int(fd)] = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
    return out


def _loop_state(client) -> dict:
    async def state() -> dict:
        loop = asyncio.get_running_loop()
        me = asyncio.current_task()
        selector = getattr(loop, "_selector", None)
        return {"tasks": sorted(t.get_name() for t in asyncio.all_tasks() if t is not me),
                "timers": len(getattr(loop, "_scheduled", [])),
                "readers": len(selector.get_map()) if selector is not None else 0}
    return client.portal.call(state)


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="needs Linux /proc")
def test_nothing_left_after_the_bot_leaves(settings, client, tmp_path, monkeypatch) -> None:
    script = tmp_path / "bot-exit"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        FAKE_MODE=exit FAKE_REPORT={tmp_path / 'report.json'} exec {sys.executable} {FAKE_BOT} "$@"
        """))
    script.chmod(0o755)
    settings.meeting_bot_path, settings.meeting_bot_runner = script, "plain"
    settings.zoom_sdk_key, settings.zoom_sdk_secret = "fictional-key", "fictional-secret-0123"
    monkeypatch.setenv("CONVO_INGEST_TOKEN", TOKEN)

    # the launcher is imported on the first request; a GET is enough and starts nothing
    assert client.get("/v1/meetings").status_code == 200
    before = {"threads": {t.ident for t in threading.enumerate()}, "children": _children(),
              "fds": _fds(), "loop": _loop_state(client)}

    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    deadline = time.monotonic() + 15
    while True:
        row = client.get(f"/v1/meetings/{mid}").json()
        loop = _loop_state(client)
        if not row["active"] and loop == before["loop"] and _children() == before["children"]:
            break
        assert time.monotonic() < deadline, (row, loop, _children())
        time.sleep(0.05)
    assert (row["state"], row["reason"]) == ("ended", "meeting_ended")

    assert client.app.state.meetings.running == {}
    assert not (settings.data_dir / "meetings" / mid).exists()
    # TestClient's own request threads (anyio workers) may come and go; nothing else
    new = [t for t in threading.enumerate() if t.ident not in before["threads"]]
    assert all("anyio" in t.name.lower() or "asyncio" in t.name.lower() for t in new), new
    # no pidfd, pipe or socket of the meeting left open in the server
    fds = _fds()
    leaked = {fd: target for fd, target in fds.items()
              if before["fds"].get(fd) != target
              and (target.startswith(("pipe:", "socket:")) or "pidfd" in target)}
    assert leaked == {}, leaked
