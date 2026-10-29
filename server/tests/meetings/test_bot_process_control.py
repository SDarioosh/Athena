"""Characterization of the meeting bot's process control: what the launcher asks of the
operating system and writes to the store, for a table of fictional meetings.

Every process call the launcher makes goes through a recording layer patched onto the
standard library itself (``subprocess.Popen``, ``os.kill``, ``os.killpg``,
``os.setpriority``, ``os.pidfd_open``, ``resource.prlimit``), so the record doesn't depend
on which module of ``athena.meetings.bot`` makes the call. The store's meeting writes, the
session ends and the API answers are recorded too. Pids, meeting ids, paths and times are
normalized; the result is compared with ``snapshots/bot_process_control.json``
(``UPDATE_SNAPSHOTS=1`` rewrites it).

The bot is tests/meetings/fake_meeting_bot.py; ``systemd-run`` and ``systemctl`` are
recorded, then replaced by what they would run (the bot, or ``true``). All data is
fictional.
"""

from __future__ import annotations

import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from athena.meetings.bot import launcher as meetings
from tests.conftest import TOKEN
from tests.meetings.test_meetings import (  # noqa: F401
    PASSCODE,
    SDK_SECRET,
    _report,
    _use,
    _wait,
    meeting_settings,
)

SNAPSHOT = Path(__file__).with_name("snapshots") / "bot_process_control.json"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
ZAK = "fictional-zak-token-0123456789"
_MID = re.compile(r"[0-9a-f]{32}")
_STD = {subprocess.DEVNULL: "DEVNULL", subprocess.PIPE: "PIPE", None: None}
_SIGNALS = {int(s): s.name for s in signal.Signals}


# what the bot (and the helpers) may keep from the server's environment
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _env_names(env: dict[str, str] | None) -> list[str] | None:
    if env is None:
        return None
    kept = sorted(k for k in env if not k.startswith("ATHENA_"))
    assert kept == sorted(k for k in KEEP_ENV if k in os.environ), kept
    assert all(env[k] == os.environ[k] for k in kept)
    return sorted(k for k in env if k.startswith("ATHENA_")) + ["<kept>"]


class Recorder:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = str(tmp_path)
        self.events: list[list[Any]] = []
        self.times: list[float] = []
        self.labels: dict[int, str] = {}
        self.fail_bot = False

    # ------------------------------------------------------------- normalizing
    def norm(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.norm_field(k, v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.norm(v) for v in value]
        if isinstance(value, str):
            value = value.replace(self.tmp, "<tmp>")
            if value.endswith("/nice") and os.path.basename(value) == "nice":
                return "nice"
            return _MID.sub("<mid>", value)
        return value

    def norm_field(self, key: str, value: Any) -> Any:
        if value is None:
            return None
        if key.endswith("_at"):
            return "<time>"
        if key == "pid":
            return self.pid(value)
        if key == "upload_token_hash":
            return "<sha256>" if re.fullmatch(r"[0-9a-f]{64}", value) else value
        return self.norm(value)

    def pid(self, pid: int) -> str:
        return self.labels.get(pid, "other")

    def add(self, *event: Any) -> None:
        self.events.append(list(event))
        self.times.append(time.monotonic())

    def take(self) -> list[list[Any]]:
        out, self.events, self.times = self.events, [], []
        return out


@pytest.fixture
def rec(tmp_path, monkeypatch, pipeline):
    r = Recorder(tmp_path)
    real_popen = subprocess.Popen

    class RecPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, argv, *args, **kwargs):  # noqa: ANN001
            argv = list(argv)
            env = kwargs.get("env")
            r.add("popen", r.norm(argv), {
                "stdin": _STD.get(kwargs.get("stdin"), "?"),
                "stdout": _STD.get(kwargs.get("stdout"), "?"),
                "stderr": _STD.get(kwargs.get("stderr"), "?"),
                "close_fds": kwargs.get("close_fds"),
                "start_new_session": kwargs.get("start_new_session", False),
                "cwd": r.norm(str(kwargs["cwd"])) if kwargs.get("cwd") else None,
                # names only, never the secrets' values; the names kept from the
                # server's environment depend on the host, so they are checked apart
                "env": _env_names(env)})
            run = argv
            name = os.path.basename(argv[0])
            if name == "systemd-run":
                run = argv[argv.index("nice"):] if "nice" in argv else ["true"]
            elif name == "systemctl":
                run = ["true"]
            bots = sum(1 for v in r.labels.values() if v.startswith("bot"))
            label = ("probe" if argv[-1:] == ["true"] else "scope-stop"
                     if name == "systemctl" else f"bot{bots + 1}")
            if r.fail_bot and label.startswith("bot"):
                raise OSError(8, "Exec format error")
            super().__init__(run, *args, **kwargs)
            r.labels[self.pid] = label

        def send_signal(self, sig):  # noqa: ANN001
            r.add("send_signal", r.pid(self.pid), _SIGNALS.get(sig, sig))
            return super().send_signal(sig)

    def wrap(mod: Any, name: str, show: Any) -> None:
        real = getattr(mod, name)

        def recorded(*args: Any) -> Any:
            r.add(*show(*args))
            return real(*args)
        monkeypatch.setattr(mod, name, recorded)

    monkeypatch.setattr(subprocess, "Popen", RecPopen)
    wrap(os, "kill", lambda pid, sig: ("kill", r.pid(pid), _SIGNALS.get(sig, sig)))
    wrap(os, "killpg", lambda pid, sig: ("killpg", r.pid(pid), _SIGNALS.get(sig, sig)))
    wrap(os, "setpriority", lambda which, pid, prio: ("setpriority", r.pid(pid), prio))
    wrap(os, "pidfd_open", lambda pid, *flags: ("pidfd_open", r.pid(pid)))
    wrap(resource, "prlimit", lambda pid, res, *limits: (
        "prlimit", r.pid(pid), "RLIMIT_AS" if res == resource.RLIMIT_AS else res,
        list(limits[0]) if limits else None))

    store = pipeline.store
    for name in ("add_meeting", "update_meeting"):
        real = getattr(store, name)

        def recorded(*args: Any, _real: Any = real, _name: str = name, **fields: Any) -> Any:
            r.add(_name, *r.norm(list(args)), r.norm(fields))
            return _real(*args, **fields)
        monkeypatch.setattr(store, name, recorded)
    real_end = pipeline.end_session

    def end_session(session_id: str, reason: str) -> Any:
        r.add("end_session", r.norm(session_id), reason)
        return real_end(session_id, reason)
    monkeypatch.setattr(pipeline, "end_session", end_session)
    return r


def _client(pipeline):
    from fastapi.testclient import TestClient

    from athena.api import create_app

    c = TestClient(create_app(pipeline))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def _answer(rec: Recorder, r: Any) -> list[Any]:
    return [r.status_code, rec.norm(r.json())]


def _settle(rec: Recorder, client: Any, timeout: float = 5.0) -> None:
    """Wait until the launcher has no running bot left (its watcher wrote the row)."""
    deadline = time.monotonic() + timeout
    while client.app.state.meetings.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not client.app.state.meetings.running


def _check(name: str, got: Any) -> None:
    data = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    if UPDATE:
        data[name] = got
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    assert name in data, f"no snapshot {name}: UPDATE_SNAPSHOTS=1 pytest {__file__}"
    assert got == data[name]


def _no_secret_logged(caplog, *secrets: str) -> None:
    text = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    for s in secrets:
        assert s and s not in text


# ------------------------------------------------------------------ scenarios
def test_start_record_and_delete(meeting_settings, pipeline, rec, tmp_path, monkeypatch,  # noqa: F811
                                 caplog) -> None:
    """Plain runner: start, recording, a duplicate start (409), DELETE; with a Zoom join
    token from the sign-in, so the waiting-room wait is capped below its lifetime."""
    caplog.set_level("DEBUG")

    async def fake_token(settings, meeting, *, transport=None):  # noqa: ANN001
        return "zak", ZAK
    monkeypatch.setattr(meetings, "join_token_provider", fake_token)
    meeting_settings.meeting_waiting_room_seconds = 9000
    rep = _use(meeting_settings, tmp_path, "record")
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        r = c.post("/v1/meetings", json={"url": f"https://zoom.us/j/81234567890?pwd={PASSCODE}",
                                         "title": "Budget sync (fictional)"})
        got["start"] = _answer(rec, r)
        mid = r.json()["id"]
        _wait(c, mid, "recording")
        info = _report(rep)
        got["recording"] = _answer(rec, c.get(f"/v1/meetings/{mid}"))
        got["duplicate"] = _answer(rec, c.post("/v1/meetings", json={"meeting_id": "987654321"}))
        got["list"] = _answer(rec, c.get("/v1/meetings"))
        got["delete"] = _answer(rec, c.delete(f"/v1/meetings/{mid}"))
        got["delete_again"] = _answer(rec, c.delete(f"/v1/meetings/{mid}"))
        got["events"] = rec.take()
    env = info["env"]
    got["bot_env"] = sorted(k for k in env if k.startswith("ATHENA_"))
    got["bot_env_kind"] = env["ATHENA_ZOOM_JOIN_TOKEN_KIND"]
    got["bot_argv"] = rec.norm(info["argv"])
    got["caps"] = {"nice": info["nice"], "rlimit_as": info["rlimit_as"],
                   "own_group": info["pgid"] == info["pid"]}
    assert env["ATHENA_ZOOM_JOIN_TOKEN"] == ZAK and env["ATHENA_MEETING_PASSCODE"] == PASSCODE
    assert not any(k.startswith("CONVO_") for k in env) and TOKEN not in json.dumps(env)
    _no_secret_logged(caplog, ZAK, PASSCODE, env["ATHENA_UPLOAD_TOKEN"],
                      env["ATHENA_MEETING_JWT"], SDK_SECRET, TOKEN)
    _check("start_record_and_delete", got)


def test_limit_reached(meeting_settings, pipeline, rec, tmp_path) -> None:  # noqa: F811
    meeting_settings.meeting_bot_max = 2
    _use(meeting_settings, tmp_path, "record")
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        ids = [c.post("/v1/meetings", json={"meeting_id": n}).json()["id"]
               for n in ("123456789", "234567891")]
        for mid in ids:
            _wait(c, mid, "recording")
        r = c.post("/v1/meetings", json={"meeting_id": "345678912"})
        assert r.json()["active"] == ids[1]
        got["third"] = _answer(rec, r)
        for mid in ids:
            c.delete(f"/v1/meetings/{mid}")
        got["after"] = [m["state"] + "/" + m["reason"] for m in
                        c.get("/v1/meetings").json()["meetings"]]
        rec.take()
    _check("limit_reached", got)


@pytest.mark.parametrize("mode", ["exit", "crash", "failed", "ownerleft"])
def test_bot_exits_by_itself(meeting_settings, pipeline, rec, tmp_path, mode) -> None:  # noqa: F811
    """Exit 0 after ``ended``, a crash (exit 3), ``failed`` during the join (exit 2)."""
    _use(meeting_settings, tmp_path, mode)
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "ended", "failed")
        _settle(rec, c)
        got["final"] = _answer(rec, c.get(f"/v1/meetings/{mid}"))
        got["events"] = rec.take()
    _check(f"exits_{mode}", got)


def test_hung_bot_is_killed_on_delete(meeting_settings, pipeline, rec, tmp_path) -> None:  # noqa: F811
    """SIGTERM, then after the grace SIGKILL to the bot's process group; DELETE answers the
    final row. The default grace plus the drain stays below the relay's 30 s wait."""
    from athena import ingest_relay

    assert meetings.STOP_GRACE_SECONDS == 10.0 and meetings.READER_DRAIN_SECONDS == 2.0
    assert meetings.STOP_GRACE_SECONDS + meetings.READER_DRAIN_SECONDS + 3.0 \
        < ingest_relay.MEETING_LEAVE_TIMEOUT.read == 30.0
    _use(meeting_settings, tmp_path, "stubborn")
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "recording")
        c.app.state.meetings.stop_grace = 0.5
        rec.take()
        started = time.monotonic()
        got["delete"] = _answer(rec, c.delete(f"/v1/meetings/{mid}"))
        took = time.monotonic() - started
        times = list(rec.times)
        got["events"] = rec.take()
    term = next(i for i, e in enumerate(got["events"]) if e[:1] == ["send_signal"])
    kill = next(i for i, e in enumerate(got["events"]) if e[:1] == ["killpg"])
    assert 0.5 <= times[kill] - times[term] < 3.0, times
    assert took < 6.0
    _check("hung_bot_delete", got)


def test_bot_does_not_start(meeting_settings, pipeline, rec, tmp_path) -> None:  # noqa: F811
    """The exec fails: 500, the row failed/bot_not_started, the session ended, no work dir."""
    _use(meeting_settings, tmp_path, "record")
    rec.fail_bot = True
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        got["start"] = _answer(rec, c.post("/v1/meetings", json={"meeting_id": "123456789"}))
        got["list"] = _answer(rec, c.get("/v1/meetings"))
        got["events"] = rec.take()
    assert not list((meeting_settings.data_dir / "meetings").iterdir())
    _check("bot_not_started", got)


def test_restart_with_stale_and_live_pids(meeting_settings, pipeline, rec, tmp_path) -> None:  # noqa: F811
    """Rows left active by an earlier server: a dead pid fails ``server_restarted`` on the
    next request; a live one (its argv has the session id) counts as busy and DELETE stops
    it with SIGTERM."""
    sid = "zoom-orphan"
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               "--session-id", sid], start_new_session=True)
    rec.labels[orphan.pid] = "orphan"
    pipeline.store.add_meeting({"id": "old", "state": "recording", "session_id": "zoom-old",
                                "meeting_id": "123456789", "pid": 2 ** 22 + 7,
                                "upload_token_hash": "x", "created_at": time.time() - 60})
    pipeline.store.add_meeting({"id": "orphan", "state": "recording", "session_id": sid,
                                "meeting_id": "123456789", "pid": orphan.pid,
                                "created_at": time.time()})
    rec.take()
    got: dict[str, Any] = {}
    try:
        with _client(pipeline) as c:
            got["list"] = _answer(rec, c.get("/v1/meetings"))
            got["busy"] = _answer(rec, c.post("/v1/meetings", json={"meeting_id": "123456789"}))
            got["delete"] = _answer(rec, c.delete("/v1/meetings/orphan"))
            got["events"] = rec.take()
        orphan.wait(timeout=5)
    finally:
        orphan.kill()
        orphan.wait()
    _check("restart_stale_and_live", got)


def test_systemd_runner(meeting_settings, pipeline, rec, tmp_path, monkeypatch) -> None:  # noqa: F811
    """Auto runner with systemd-run found: the probe, the scope's caps, and the scope stop
    after the bot exited (no prlimit: systemd caps it)."""
    monkeypatch.setattr(meetings.shutil, "which", lambda name: f"/usr/bin/{name}")
    meeting_settings.meeting_bot_runner = "auto"
    meeting_settings.meeting_bot_cpu, meeting_settings.meeting_bot_memory = "150%", "1536M"
    _use(meeting_settings, tmp_path, "record")
    got: dict[str, Any] = {}
    with _client(pipeline) as c:
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "recording")
        got["delete"] = _answer(rec, c.delete(f"/v1/meetings/{mid}"))
        _settle(rec, c)
        deadline = time.monotonic() + 5
        while ["pidfd_open", "scope-stop"] not in rec.events:  # the scope stop's last call
            assert time.monotonic() < deadline
            time.sleep(0.05)
        got["events"] = rec.take()
    _check("systemd_runner", got)


def test_shutdown_as_container_init(meeting_settings, pipeline, rec, tmp_path,  # noqa: F811
                                    monkeypatch) -> None:
    monkeypatch.setattr(meetings, "_exit_kills_bots", lambda: True)
    _use(meeting_settings, tmp_path, "record")
    with _client(pipeline) as c:
        mid = c.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
        _wait(c, mid, "recording")
        rec.take()
    got = {"events": rec.take(),
           "row": rec.norm(pipeline.store.get_meeting(mid))}
    _check("shutdown_container_init", got)


def test_launcher_module_names() -> None:
    """Names other code and tests reach through the launcher module (and the package)."""
    import athena.meetings as package

    names = ("Launcher", "MeetingError", "ZoomMeeting", "parse_zoom_link",
             "parse_join_request", "sign_sdk_jwt", "join_token_provider", "memory_bytes",
             "bot_args", "build_command", "bot_env", "check_settings", "parse_state_line",
             "final_state", "runtime_limit_kill", "LineSplitter", "out", "_alive",
             "_Running", "_exit_kills_bots", "_watch_exit", "_close_pidfd",
             "_unwatch_exit", "_run_quick", "STATES", "TERMINAL", "DEVICE", "BOT_NAME",
             "STOP_GRACE_SECONDS", "LIFETIME_GRACE_SECONDS", "SYSTEMD_MARGIN_SECONDS",
             "READER_DRAIN_SECONDS", "MAX_LOG_LINE", "MAX_TITLE_CHARS",
             "MAX_PASSCODE_CHARS", "JOIN_TOKEN_SECONDS", "JOIN_TOKEN_MARGIN_SECONDS",
             "MAX_BOT_SECONDS", "SHUTDOWN_STOP_SECONDS", "log")
    for name in names:
        assert getattr(package, name) is getattr(meetings, name), name
    assert meetings.MAX_LOG_LINE == 4096 and meetings.LineSplitter().limit == 4096
