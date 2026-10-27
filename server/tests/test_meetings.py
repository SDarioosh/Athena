"""Meeting bot launcher (lane 1B): links, join token, routes, bot lifecycle, idle cost.

The bot is tests/fake_meeting_bot.py behind a wrapper script; nothing joins Zoom. All
data is fictional (meeting numbers, keys, passcodes).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from conftest import TOKEN, tone, wav

from convo_server import meetings
from convo_server.meetings import (
    ZoomMeeting,
    bot_args,
    build_command,
    final_state,
    memory_bytes,
    parse_join_request,
    parse_state_line,
    parse_zoom_link,
    sign_sdk_jwt,
)

FAKE_BOT = Path(__file__).with_name("fake_meeting_bot.py")
SDK_KEY = "fictional-sdk-key"
SDK_SECRET = "fictional-sdk-secret-0123456789"
PASSCODE = "Zz9fictional.1"


# ------------------------------------------------------------------------ links
@pytest.mark.parametrize("link, number, passcode", [
    ("https://zoom.us/j/123456789?pwd=abcDEF.1", "123456789", "abcDEF.1"),
    ("https://us06web.zoom.us/j/81234567890?pwd=Zz09.1#success", "81234567890", "Zz09.1"),
    ("https://acme.zoom.us/j/1234567890", "1234567890", None),
    ("http://zoom.us/j/123456789", "123456789", None),
    ("  <https://zoom.us/j/123456789?pwd=x>  ", "123456789", "x"),
    ("zoom.us/j/123456789?pwd=x", "123456789", "x"),
    ("us02web.zoom.us/j/123456789", "123456789", None),
    ("https://zoom.us/wc/join/123456789?pwd=x", "123456789", "x"),
    ("https://zoom.us/wc/123456789/join?pwd=x", "123456789", "x"),
    ("zoommtg://zoom.us/join?action=join&confno=123456789&pwd=x", "123456789", "x"),
    ("zoomus://acme.zoom.us/join?confno=98765432101", "98765432101", None),
    ("HTTPS://ACME.ZOOM.US/j/123456789", "123456789", None),
])
def test_links_accepted(link: str, number: str, passcode: str | None) -> None:
    assert parse_zoom_link(link) == ZoomMeeting(number, passcode)


@pytest.mark.parametrize("link, message", [
    ("", "paste"),
    ("https://zoom.us/my/dana.fictional", "personal links"),
    ("https://zoom.us/w/123456789?tk=abc", "webinars"),
    ("https://zoom.us/s/123456789", "start link"),
    ("zoommtg://zoom.us/start?confno=123456789", "start link"),
    ("https://zoom.us/meeting/register/tJ0fictional", "registration"),
    ("https://zoom.us/j/12345", "9 to 11 digits"),
    ("https://zoom.us/j/123456789012", "9 to 11 digits"),
    ("https://zoom.us/j/12345678a", "9 to 11 digits"),
    ("https://zoomgov.com/j/123456789", "Zoom for Government"),
    ("https://zoom.us.evil.example/j/123456789", "not a Zoom"),
    ("https://evil.example/zoom.us/j/123456789", "not a Zoom"),
    ("https://user@zoom.us/j/123456789", "not a Zoom"),
    ("https://zoom.us:8443/j/123456789", "not a Zoom"),
    ("ftp://zoom.us/j/123456789", "not a Zoom"),
    ("https://zoom.us/j/123456789/extra", "not a Zoom"),
    ("https://zoom.us/", "not a Zoom"),
    ("zoommtg://zoom.us/join?action=join", "confno"),
    ("https://meet.google.com/abc-defg-hij", "not a Zoom"),
    ("https://zoom.us/j/123456789?pwd=has space", "passcode"),
])
def test_links_refused(link: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_zoom_link(link)


def test_join_request_forms() -> None:
    assert parse_join_request(None, "123 4567 8901", "secret") == \
        ZoomMeeting("12345678901", "secret")
    assert parse_join_request(None, 123456789, None) == ZoomMeeting("123456789", None)
    assert parse_join_request(None, "123-456-789", "") == ZoomMeeting("123456789", None)
    # a passcode beside a link without pwd is used; the link's own pwd wins
    assert parse_join_request("https://zoom.us/j/123456789", None, "p") == \
        ZoomMeeting("123456789", "p")
    assert parse_join_request("https://zoom.us/j/123456789?pwd=q", None, "p").passcode == "q"
    with pytest.raises(ValueError, match="not both"):
        parse_join_request("https://zoom.us/j/123456789", "123456789", None)
    with pytest.raises(ValueError, match="send url"):
        parse_join_request(None, None, None)


# ----------------------------------------------------------------- join token
def _decode(token: str) -> tuple[dict, dict, bytes, str]:
    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    head, payload, sig = token.split(".")
    return json.loads(unb64(head)), json.loads(unb64(payload)), unb64(sig), f"{head}.{payload}"


def test_sdk_jwt_signed_locally() -> None:
    token = sign_sdk_jwt(SDK_KEY, SDK_SECRET, "81234567890", now=1_800_000_000,
                         lifetime=4 * 3600 + 3600)
    header, payload, sig, signed = _decode(token)
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert hmac.compare_digest(
        sig, hmac.new(SDK_SECRET.encode(), signed.encode(), hashlib.sha256).digest())
    assert payload == {"appKey": SDK_KEY, "mn": 81234567890, "role": 0,
                       "iat": 1_800_000_000 - 30, "exp": 1_800_000_000 - 30 + 5 * 3600,
                       "tokenExp": 1_800_000_000 - 30 + 5 * 3600}


def test_sdk_jwt_lifetime_within_zoom_limits() -> None:
    short = _decode(sign_sdk_jwt(SDK_KEY, SDK_SECRET, "123456789", now=0, lifetime=60))[1]
    assert short["exp"] - short["iat"] == 1800
    long = _decode(sign_sdk_jwt(SDK_KEY, SDK_SECRET, "123456789", now=0, lifetime=1e9))[1]
    assert long["exp"] - long["iat"] == 48 * 3600


# --------------------------------------------------------- pure helpers
def test_state_lines() -> None:
    assert parse_state_line("STATE recording\n") == ("recording", None)
    assert parse_state_line("STATE recording no_audio") == ("recording", "no_audio")
    assert parse_state_line("STATE failed wrong_passcode") == ("failed", "wrong_passcode")
    assert parse_state_line("STATE waiting_permission") == ("waiting_permission", None)
    assert parse_state_line("STATE waiting_permission denied") == \
        ("waiting_permission", "denied")
    for junk in ("STATE recording_denied", "STATE recording No Audio", "state ended",
                 "STATE ended a b", "hello", ""):
        assert parse_state_line(junk) is None


def test_final_state() -> None:
    assert final_state(("ended", "alone"), 0, None) == ("ended", "alone")
    assert final_state(("failed", "wrong_passcode"), 2, "stopped") == \
        ("failed", "wrong_passcode")
    assert final_state(("recording", None), -15, "stopped") == ("ended", "stopped")
    assert final_state(("recording", None), 0, None) == ("ended", "exited")
    assert final_state(("recording", None), 3, None) == ("failed", "bot_crashed")
    assert final_state(None, -9, None) == ("failed", "bot_crashed")


def test_memory_bytes() -> None:
    assert memory_bytes("2G") == 2 * 1024 ** 3
    assert memory_bytes("1536m") == 1536 * 1024 ** 2
    assert memory_bytes("4096") == 4096
    with pytest.raises(ValueError):
        memory_bytes("2 GB")


def test_systemd_command_caps(settings) -> None:
    settings.meeting_bot_cpu, settings.meeting_bot_memory = "150%", "1G"
    args = bot_args(settings, ZoomMeeting("123456789", PASSCODE), "zoom-x", Path("/w"))
    argv = build_command(settings, "systemd", Path("/opt/bot"), args, "athena-meeting-x")
    assert argv[:5] == ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    assert "--unit=athena-meeting-x" in argv
    assert ["-p", "CPUQuota=150%", "-p", "MemoryMax=1G",
            "-p", f"RuntimeMaxSec={int(settings.max_session_seconds) + 90}"] == \
        argv[6:12]
    assert argv[12:16] == ["nice", "-n", "10", "/opt/bot"]
    assert PASSCODE not in " ".join(argv)
    assert args == ["--meeting-id", "123456789", "--session-id", "zoom-x",
                    "--upload-url", "http://127.0.0.1:8000", "--name", "Athena",
                    "--max-seconds", "600", "--alone-seconds", "60",
                    "--waiting-room-seconds", "600", "--codec", "opus", "--work-dir", "/w"]


def test_upload_url_is_loopback_when_bound_to_all(settings) -> None:
    settings.host, settings.port = "0.0.0.0", 8123
    args = bot_args(settings, ZoomMeeting("123456789"), "zoom-x", Path("/w"))
    assert args[args.index("--upload-url") + 1] == "http://127.0.0.1:8123"


def test_join_token_hook_none_without_signin(settings) -> None:
    """Mode auto without a sign-in, and mode none: no join token, no network."""
    for mode in ("auto", "none"):
        settings.zoom_join_token = mode
        assert asyncio.run(meetings.join_token_provider(
            settings, ZoomMeeting("123456789"))) == ("none", None)


# ------------------------------------------------------------------ routes
def _bot(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    script = tmp_path / f"bot-{mode}"
    rep = tmp_path / f"report-{mode}.json"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        FAKE_MODE={mode} FAKE_REPORT={rep} exec {sys.executable} {FAKE_BOT} "$@"
        """))
    script.chmod(0o755)
    return script, rep


@pytest.fixture
def meeting_settings(settings, tmp_path, monkeypatch):
    settings.meeting_bot_path, _ = _bot(tmp_path, "record")
    settings.meeting_bot_runner = "plain"
    settings.zoom_sdk_key, settings.zoom_sdk_secret = SDK_KEY, SDK_SECRET
    # a server environment that holds the ingest token: the bot must never see it
    monkeypatch.setenv("CONVO_INGEST_TOKEN", TOKEN)
    return settings


def _use(settings, tmp_path: Path, mode: str) -> Path:
    settings.meeting_bot_path, rep = _bot(tmp_path, mode)
    return rep


def _wait(client, mid: str, *states: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        row = client.get(f"/v1/meetings/{mid}").json()
        if row["state"] in states:
            return row
        assert time.monotonic() < deadline, row
        time.sleep(0.05)


def _report(path: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return json.loads(path.read_text())


def _gone(pid: int) -> bool:
    """Exited and reaped: no process and no zombie."""
    return not Path(f"/proc/{pid}").exists()


def _tasks(client) -> list[str]:
    async def names() -> list[str]:
        me = asyncio.current_task()
        return [t.get_name() for t in asyncio.all_tasks() if t is not me]
    return [n for n in client.portal.call(names) if n.startswith("meeting-")]


def _launcher(client):
    return client.app.state.meetings


def test_join_record_leave(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "record")
    r = client.post("/v1/meetings", json={
        "url": f"https://us06web.zoom.us/j/81234567890?pwd={PASSCODE}",
        "title": "Budget sync (fictional)"})
    assert r.status_code == 202, r.text
    body = r.json()
    mid = body["id"]
    assert body["state"] == "starting" and body["session_id"] == f"zoom-{mid}"
    row = _wait(client, mid, "recording")
    assert row["started_at"] and row["active"] and row["reason"] is None
    assert row["meeting_id"] == "81234567890" and row["title"] == "Budget sync (fictional)"
    info = _report(rep)
    assert _tasks(client) == [f"meeting-{mid}"]

    # secrets reach the bot by environment only, never argv; no server secrets
    argv = " ".join(info["argv"])
    env = info["env"]
    assert env["ATHENA_MEETING_PASSCODE"] == PASSCODE and PASSCODE not in argv
    assert env["ATHENA_UPLOAD_TOKEN"] and env["ATHENA_UPLOAD_TOKEN"] not in argv
    assert env["ATHENA_MEETING_JWT"].count(".") == 2 and env["ATHENA_MEETING_JWT"] not in argv
    assert "ATHENA_ZOOM_JOIN_TOKEN" not in env  # the hook returns None today
    assert TOKEN not in json.dumps(info) and not any(k.startswith("CONVO_") for k in env)
    assert SDK_SECRET not in json.dumps(info)
    assert info["argv"][:4] == ["--meeting-id", "81234567890", "--session-id", f"zoom-{mid}"]
    # safety caps of the plain runner, own process group, its own work dir
    assert info["nice"] >= 10
    assert info["rlimit_as"] == 2 * 1024 ** 3
    assert info["pgid"] == info["pid"]
    assert Path(info["cwd"]).name == mid and Path(info["cwd"]).is_dir()
    # the passcode is not stored
    raw = client.app.state.meetings.store._q("SELECT * FROM meetings")
    assert PASSCODE not in json.dumps(raw)

    r = client.delete(f"/v1/meetings/{mid}")
    assert r.status_code == 200
    assert r.json()["state"] == "ended" and r.json()["reason"] == "stopped"
    assert r.json()["ended_at"] and not r.json()["active"]
    # nothing left: process reaped, watcher task gone, work dir removed
    assert _gone(info["pid"])
    assert _tasks(client) == [] and _launcher(client).running == {}
    assert not Path(info["cwd"]).exists()
    # DELETE again is a no-op
    assert client.delete(f"/v1/meetings/{mid}").json()["state"] == "ended"


def test_bot_leaves_on_its_own(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "exit")
    mid = client.post("/v1/meetings", json={"meeting_id": "123 456 789"}).json()["id"]
    row = _wait(client, mid, "ended", "failed")
    assert (row["state"], row["reason"]) == ("ended", "meeting_ended")
    pid = _report(rep)["pid"]
    deadline = time.monotonic() + 5
    while _tasks(client) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(pid) and _tasks(client) == []
    assert "ATHENA_MEETING_PASSCODE" not in _report(rep)["env"]  # none given


def test_bot_crash_is_failed(meeting_settings, client, tmp_path) -> None:
    _use(meeting_settings, tmp_path, "crash")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "ended", "failed")
    assert (row["state"], row["reason"]) == ("failed", "bot_crashed")


def test_bot_failure_reason(meeting_settings, client, tmp_path) -> None:
    _use(meeting_settings, tmp_path, "failed")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "ended", "failed")
    assert (row["state"], row["reason"]) == ("failed", "wrong_passcode")
    assert row["started_at"] is None


def test_no_audio_reason(meeting_settings, client, tmp_path) -> None:
    _use(meeting_settings, tmp_path, "noaudio")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "recording")
    assert row["reason"] == "no_audio"
    client.delete(f"/v1/meetings/{mid}")


def test_stubborn_bot_is_killed(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "stubborn")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    _wait(client, mid, "recording")
    _launcher(client).stop_grace = 0.5
    pid = _report(rep)["pid"]
    r = client.delete(f"/v1/meetings/{mid}").json()
    assert (r["state"], r["reason"]) == ("ended", "stopped")
    assert _gone(pid) and _tasks(client) == []


def test_helpers_left_by_the_bot_are_killed(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "child")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    _wait(client, mid, "recording")
    child = _report(rep)["child"]
    assert not _gone(child)
    client.delete(f"/v1/meetings/{mid}")
    deadline = time.monotonic() + 5
    while not _gone(child) and time.monotonic() < deadline:
        # the orphaned helper is reparented; its new parent reaps it
        time.sleep(0.05)
    status = Path(f"/proc/{child}/status")
    assert _gone(child) or "zombie" in status.read_text()


def test_busy(meeting_settings, client, tmp_path) -> None:
    _use(meeting_settings, tmp_path, "record")
    first = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    r = client.post("/v1/meetings", json={"meeting_id": "987654321"})
    assert r.status_code == 409
    assert r.json()["detail"] == "busy" and r.json()["active"] == first
    assert "already in a meeting" in r.json()["message"]
    listed = client.get("/v1/meetings").json()["meetings"]
    assert [m["id"] for m in listed] == [first]
    client.delete(f"/v1/meetings/{first}")
    r = client.post("/v1/meetings", json={"meeting_id": "987654321"})
    assert r.status_code == 202
    client.delete(f"/v1/meetings/{r.json()['id']}")


def test_off_without_bot_path(client) -> None:
    r = client.post("/v1/meetings", json={"url": "https://zoom.us/j/123456789"})
    assert r.status_code == 501 and "CONVO_MEETING_BOT_PATH" in r.json()["detail"]


def test_off_without_sdk_key(meeting_settings, client) -> None:
    meeting_settings.zoom_sdk_secret = None
    r = client.post("/v1/meetings", json={"url": "https://zoom.us/j/123456789"})
    assert r.status_code == 501 and "CONVO_ZOOM_SDK" in r.json()["detail"]


def test_bad_link(meeting_settings, client) -> None:
    r = client.post("/v1/meetings", json={"url": "https://meet.google.com/abc-defg-hij"})
    assert r.status_code == 400 and "Zoom" in r.json()["detail"]
    r = client.post("/v1/meetings", json={"url": "https://zoom.us/my/dana"})
    assert r.status_code == 400 and "personal" in r.json()["detail"]
    assert client.get("/v1/meetings").json()["meetings"] == []


def test_routes_need_the_token(meeting_settings, client) -> None:
    for method, path in (("post", "/v1/meetings"), ("get", "/v1/meetings"),
                         ("get", "/v1/meetings/x"), ("delete", "/v1/meetings/x")):
        r = getattr(client, method)(path, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
    assert client.get("/v1/meetings/unknown").status_code == 404
    assert client.delete("/v1/meetings/unknown").status_code == 404


def _chunk(token: str, session_id: str, device: str = "zoom", seq: int = 0):
    return dict(files={"audio": ("c.wav", wav(tone(300, 1.0)), "audio/wav")},
                data={"device": device, "session_id": session_id, "sequence": str(seq),
                      "timestamp": "1800000000", "codec": "wav", "track": "16778240",
                      "track_name": "Dana (fictional)"},
                headers={"Authorization": f"Bearer {token}"})


def test_upload_token_only_for_its_session(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "record")
    body = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()
    mid, sid = body["id"], body["session_id"]
    _wait(client, mid, "recording")
    bot_token = _report(rep)["env"]["ATHENA_UPLOAD_TOKEN"]

    r = client.post("/v1/ingest/chunk", **_chunk(bot_token, sid))
    assert r.status_code == 200 and r.json()["ok"] and r.json()["track"] == "16778240"
    assert client.post("/v1/ingest/chunk", **_chunk(bot_token, "other")).status_code == 403
    assert client.post("/v1/ingest/chunk",
                       **_chunk(bot_token, sid, device="omi", seq=1)).status_code == 403
    auth = {"Authorization": f"Bearer {bot_token}"}
    for method, path in (("get", "/v1/sessions"), ("get", f"/v1/sessions/{sid}"),
                         ("get", "/v1/meetings"), ("post", "/v1/sessions/other/end"),
                         ("delete", f"/v1/meetings/{mid}")):
        assert getattr(client, method)(path, headers=auth).status_code in (401, 403), path
    assert client.post("/v1/ingest/chunk.json", headers=auth, json={}).status_code == 403
    # the bot may post its own meeting's chat, never another session's
    chat = {"topic": "Planning (fictional)", "lines": []}
    r = client.post(f"/v1/sessions/{sid}/chat", headers=auth, json=chat)
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert client.post("/v1/sessions/other/chat", headers=auth,
                       json=chat).status_code == 403
    assert client.post(f"/v1/sessions/{sid}/end", headers=auth).status_code == 200
    # the token dies with the bot
    client.delete(f"/v1/meetings/{mid}")
    assert client.post("/v1/ingest/chunk", **_chunk(bot_token, sid, seq=2)).status_code == 401
    # the session was ended (by the bot here; the launcher makes sure otherwise)
    assert client.get(f"/v1/sessions/{sid}").json()["status"] == "ended"


def test_launcher_ends_the_session(meeting_settings, client, tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "crash")
    body = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()
    _wait(client, body["id"], "recording", "failed")
    token = _report(rep)["env"]["ATHENA_UPLOAD_TOKEN"]
    client.post("/v1/ingest/chunk", **_chunk(token, body["session_id"]))
    _wait(client, body["id"], "failed")
    sess = client.get(f"/v1/sessions/{body['session_id']}").json()
    assert sess["status"] == "ended" and sess["stop_reason"] == "meeting_bot_crashed"


def test_restart_reconciles_lazily(meeting_settings, pipeline, tmp_path) -> None:
    """A row left active by an earlier server process doesn't block new meetings."""
    from fastapi.testclient import TestClient

    from convo_server.api import create_app

    store = pipeline.store
    store.add_meeting({"id": "old", "state": "recording", "session_id": "zoom-old",
                       "meeting_id": "123456789", "pid": 2 ** 22 + 7,  # no such process
                       "upload_token_hash": "x", "created_at": time.time() - 60})
    _use(meeting_settings, tmp_path, "record")
    with TestClient(create_app(pipeline)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        assert c.get("/v1/meetings/old").json()["reason"] == "server_restarted"
        r = c.post("/v1/meetings", json={"meeting_id": "123456789"})
        assert r.status_code == 202
        c.delete(f"/v1/meetings/{r.json()['id']}")
    assert store.get_meeting("old")["upload_token_hash"] is None


def test_restart_keeps_a_live_orphan(meeting_settings, pipeline, tmp_path) -> None:
    """A bot still running from before a restart counts as busy and DELETE stops it."""
    from fastapi.testclient import TestClient

    from convo_server.api import create_app

    sid = "zoom-orphan"
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               "--session-id", sid], start_new_session=True)
    try:
        pipeline.store.add_meeting({"id": "orphan", "state": "recording", "session_id": sid,
                                    "meeting_id": "123456789", "pid": orphan.pid,
                                    "created_at": time.time()})
        with TestClient(create_app(pipeline)) as c:
            c.headers["Authorization"] = f"Bearer {TOKEN}"
            assert c.post("/v1/meetings",
                          json={"meeting_id": "123456789"}).status_code == 409
            r = c.delete("/v1/meetings/orphan").json()
            assert (r["state"], r["reason"]) == ("ended", "stopped")
            orphan.wait(timeout=5)
    finally:
        orphan.kill()
        orphan.wait()


# ------------------------------------------------------------------ idle cost
def test_api_import_starts_nothing_and_skips_meeting_code(tmp_path) -> None:
    """Importing the API and creating the app starts no thread and doesn't import
    convo_server.meetings (a fresh interpreter, so other tests can't pollute it)."""
    code = textwrap.dedent(f"""
        import sys, threading
        from convo_server.config import Settings
        from convo_server.pipeline import Pipeline
        from convo_server.store import Store
        from convo_server.vad import EnergyVAD
        from convo_server.api import create_app
        s = Settings(ingest_token="x" * 20, data_dir={str(tmp_path)!r}, run_worker=False,
                     vad_backend="energy", meeting_bot_path="/bin/true")
        app = create_app(Pipeline(s, Store(s.db_path), EnergyVAD()))
        assert threading.active_count() == 1, threading.enumerate()
        from fastapi.testclient import TestClient
        c = TestClient(app)
        assert c.get("/health").json() == {{"ok": True}}
        assert c.post("/v1/ingest/chunk").status_code == 401
        assert "convo_server.meetings" not in sys.modules, "meetings imported"
        assert "convo_server.zoom_signin" not in sys.modules, "zoom sign-in imported"
        # TestClient's own portal thread may take a moment to finish
        for t in threading.enumerate():
            if t is not threading.main_thread():
                t.join(5)
        assert threading.active_count() == 1, threading.enumerate()
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parents[1], timeout=60)
    assert r.stdout.strip() == "ok", r.stderr


def test_no_thread_during_a_meeting(meeting_settings, client, tmp_path) -> None:
    """The bot's watcher is an asyncio task, not a thread."""
    import threading

    before = {t.ident for t in threading.enumerate()}
    _use(meeting_settings, tmp_path, "record")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    _wait(client, mid, "recording")
    during = {t.ident for t in threading.enumerate()} - before
    # only anyio's request worker threads may appear (TestClient), none named for us
    assert not [t for t in threading.enumerate()
                if t.ident in during and "meeting" in t.name.lower()]
    assert all("waitpid" not in t.name.lower() for t in threading.enumerate())
    client.delete(f"/v1/meetings/{mid}")


def test_meeting_secret_env_names_documented() -> None:
    doc = meetings.__doc__ or ""
    for name in ("ATHENA_MEETING_JWT", "ATHENA_UPLOAD_TOKEN", "ATHENA_MEETING_PASSCODE",
                 "ATHENA_ZOOM_JOIN_TOKEN", "STATE <state>"):
        assert name in doc
    assert os.path.exists(FAKE_BOT)


@pytest.mark.parametrize("probe, runner", [(0, "systemd"), (1, "plain")])
def test_auto_runner_probes_once(meeting_settings, pipeline, monkeypatch, probe,
                                 runner) -> None:
    calls = []

    async def fake_run(argv, env, timeout=5.0):
        calls.append(argv)
        return probe

    monkeypatch.setattr(meetings, "_run_quick", fake_run)
    monkeypatch.setattr(meetings.shutil, "which", lambda name: f"/usr/bin/{name}")
    meeting_settings.meeting_bot_runner = "auto"
    launcher = meetings.Launcher(pipeline)

    async def twice():
        return [await launcher._pick_runner(), await launcher._pick_runner()]

    assert asyncio.run(twice()) == [runner, runner]
    assert calls == [["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"]]


def test_meetings_table_and_token_lookup(tmp_path) -> None:
    from convo_server.store import Store

    store = Store(tmp_path / "convo.db")
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(meetings)")}
    assert {"id", "state", "reason", "session_id", "meeting_id", "pid",
            "upload_token_hash"} <= cols
    assert not any("pass" in c or "pwd" in c for c in cols)  # never stored
    digest = hashlib.sha256(b"bot-token").hexdigest()
    store.add_meeting({"id": "m1", "state": "recording", "session_id": "zoom-m1",
                       "meeting_id": "123456789", "upload_token_hash": digest,
                       "created_at": 1.0})
    assert store.meeting_upload_session("bot-token") == "zoom-m1"
    assert store.meeting_upload_session("other") is None
    store.update_meeting("m1", state="ended")
    assert store.meeting_upload_session("bot-token") is None


def test_permission_granted_later(meeting_settings, client, tmp_path) -> None:
    _use(meeting_settings, tmp_path, "permgrant")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "waiting_permission")
    assert row["active"] and row["reason"] is None and row["started_at"] is None
    # waiting for permission counts as busy
    assert client.post("/v1/meetings", json={"meeting_id": "987654321"}).status_code == 409
    row = _wait(client, mid, "recording")
    assert row["started_at"] and row["reason"] is None
    client.delete(f"/v1/meetings/{mid}")


def test_permission_denied_keeps_waiting(meeting_settings, client, tmp_path) -> None:
    meeting_settings.meeting_waiting_room_seconds = 1  # not applied to waiting_permission
    _use(meeting_settings, tmp_path, "permdenied")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    deadline = time.monotonic() + 5
    row = _wait(client, mid, "waiting_permission")
    while row["reason"] != "denied":
        assert time.monotonic() < deadline, row
        row = client.get(f"/v1/meetings/{mid}").json()
    time.sleep(1.2)
    row = client.get(f"/v1/meetings/{mid}").json()
    assert (row["state"], row["reason"], row["active"]) == ("waiting_permission", "denied",
                                                            True)
    out = client.delete(f"/v1/meetings/{mid}").json()
    assert (out["state"], out["reason"]) == ("ended", "stopped")
    assert out["started_at"] is None


def test_already_allowed_goes_straight_to_recording(meeting_settings, client,
                                                    tmp_path) -> None:
    rep = _use(meeting_settings, tmp_path, "record")  # host, co-host or already allowed
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    assert _wait(client, mid, "recording", "waiting_permission")["state"] == "recording"
    _report(rep)
    client.delete(f"/v1/meetings/{mid}")


# ------------------------------------------------- QA regressions (Antigravity audit)
def test_qa17_long_title_or_passcode_is_400_with_a_string(meeting_settings, client) -> None:
    """BUG-17: the documented 400 {"detail": "<message>"}, never pydantic's 422 list."""
    url = "https://zoom.us/j/85012345678"
    r = client.post("/v1/meetings", json={"url": url, "title": "x" * 201})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "title is longer than 200 characters"
    r = client.post("/v1/meetings", json={"meeting_id": "85012345678",
                                          "passcode": "p" * 129})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str), r.text
    assert "passcode" in r.json()["detail"]
    r = client.post("/v1/meetings", json={"url": url, "meeting_id": ["x"]})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str), r.text
    r = client.post("/v1/meetings", content=b"[1, 2]",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str), r.text
    # a hard cap on the body stays
    r = client.post("/v1/meetings", json={"url": url, "title": "x" * 20_000})
    assert r.status_code == 413 and isinstance(r.json()["detail"], str), r.text
    assert client.get("/v1/meetings").json()["meetings"] == []
    # spaces around a title that fits are stripped, not counted
    padded = "  " + "t" * 195 + "      "
    r = client.post("/v1/meetings", json={"url": url, "title": padded})
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    assert client.get(f"/v1/meetings/{mid}").json()["title"] == "t" * 195
    client.delete(f"/v1/meetings/{mid}")


def test_qa18_systemd_limit_fires_after_the_launcher_timeout(settings) -> None:
    """BUG-18: RuntimeMaxSec is past the launcher's own lifetime plus its SIGTERM grace,
    so the launcher's timeout (max_duration) always fires first."""
    for seconds in (5, 5.9, 600, 14400):
        settings.max_session_seconds = seconds
        argv = build_command(settings, "systemd", Path("/opt/bot"), [], "u")
        limit = int(next(a for a in argv if a.startswith("RuntimeMaxSec="))
                    .split("=", 1)[1])
        lifetime = seconds + meetings.LIFETIME_GRACE_SECONDS
        assert limit >= lifetime + meetings.STOP_GRACE_SECONDS + 10, (seconds, limit)


def test_qa18_runtime_limit_kill_is_max_duration(meeting_settings, client, tmp_path,
                                                 monkeypatch) -> None:
    """BUG-18: a bot killed by a signal after its max duration (systemd's RuntimeMaxSec)
    is ended/max_duration, not failed/bot_crashed."""
    import signal as sig

    monkeypatch.setattr(meetings, "LIFETIME_GRACE_SECONDS", 100.0)  # launcher waits
    meeting_settings.max_session_seconds = 1
    rep = _use(meeting_settings, tmp_path, "stubborn")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    _wait(client, mid, "recording")
    pid = _report(rep)["pid"]
    time.sleep(1.2)
    os.kill(pid, sig.SIGKILL)  # what systemd does at RuntimeMaxSec
    row = _wait(client, mid, "ended", "failed")
    assert (row["state"], row["reason"]) == ("ended", "max_duration")


def test_qa19_delete_answers_the_final_row(meeting_settings, client, tmp_path,
                                           monkeypatch) -> None:
    """BUG-19: DELETE answers the final row once the bot exited, even when the scope
    cleanup afterwards is slow (systemctl --user stop can take up to 5 s)."""
    slow = []

    async def slow_stop(argv, env, timeout=5.0):
        slow.append(argv)
        await asyncio.sleep(5.5)
        return 0

    monkeypatch.setattr(meetings, "_run_quick", slow_stop)
    monkeypatch.setattr(meetings, "build_command",
                        lambda settings, runner, bot, args, unit: [str(bot), *args])
    meeting_settings.meeting_bot_runner = "systemd"
    rep = _use(meeting_settings, tmp_path, "record")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    _wait(client, mid, "recording")
    started = time.monotonic()
    r = client.delete(f"/v1/meetings/{mid}").json()
    assert (r["state"], r["reason"], r["active"]) == ("ended", "stopped", False), r
    assert time.monotonic() - started < 5.0
    assert _gone(_report(rep)["pid"])
    deadline = time.monotonic() + 10
    while _tasks(client) and time.monotonic() < deadline:  # the slow cleanup finishes
        time.sleep(0.1)
    assert _tasks(client) == [] and slow and slow[0][:3] == ["systemctl", "--user", "stop"]
    assert client.get(f"/v1/meetings/{mid}").json()["state"] == "ended"


def test_qa20_over_long_line_is_dropped_whole(meeting_settings, client, tmp_path) -> None:
    """BUG-20: a log line over the limit is discarded up to its newline; a STATE text
    in its tail is never read as a status line."""
    _use(meeting_settings, tmp_path, "longline")
    mid = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()["id"]
    row = _wait(client, mid, "recording", "failed", "ended")
    assert row["state"] == "recording", row
    r = client.delete(f"/v1/meetings/{mid}").json()
    assert (r["state"], r["reason"]) == ("ended", "stopped")


def test_qa22_bot_token_uploads_conversation_only(meeting_settings, client,
                                                  tmp_path) -> None:
    """BUG-22: the per-meeting token can't make the meeting a voice note."""
    rep = _use(meeting_settings, tmp_path, "record")
    body = client.post("/v1/meetings", json={"meeting_id": "123456789"}).json()
    mid, sid = body["id"], body["session_id"]
    _wait(client, mid, "recording")
    bot_token = _report(rep)["env"]["ATHENA_UPLOAD_TOKEN"]
    req = _chunk(bot_token, sid)
    req["data"]["kind"] = "note"
    r = client.post("/v1/ingest/chunk", **req)
    assert r.status_code == 403, r.text
    assert client.get(f"/v1/sessions/{sid}").status_code == 404  # nothing stored
    req["data"]["kind"] = "conversation"
    assert client.post("/v1/ingest/chunk", **req).status_code == 200
    client.delete(f"/v1/meetings/{mid}")


@pytest.mark.parametrize("link, number", [
    ("https://zoom.us/wc/JOIN/85012345678", "85012345678"),
    ("https://zoom.us/WC/85012345678/Join", "85012345678"),
    ("https://zoom.us/J/85012345678", "85012345678"),
    ("zoommtg://zoom.us/JOIN?confno=85012345678", "85012345678"),
    ("ZOOMUS://zoom.us/Join?confno=85012345678", "85012345678"),
])
def test_qa25_path_segments_case_insensitive(link: str, number: str) -> None:
    assert parse_zoom_link(link).meeting_id == number


@pytest.mark.parametrize("link", [
    "zoomgov.com/j/1612345678", "https://zoomgov.com/j/1612345678",
    "acme.zoomgov.com/j/1612345678", "zoommtg://zoomgov.com/join?confno=1612345678",
])
def test_qa25_zoomgov_has_its_own_message(link: str) -> None:
    with pytest.raises(ValueError, match="Zoom for Government"):
        parse_zoom_link(link)
    with pytest.raises(ValueError, match="start link"):
        parse_zoom_link("zoommtg://zoom.us/START?confno=85012345678")


def test_qa20_line_splitter() -> None:
    lines = meetings.LineSplitter(limit=20)
    assert lines.feed(b"STATE rec") == []
    assert lines.feed(b"ording\nab\n") == [b"STATE recording", b"ab"]
    assert lines.feed(b"x" * 21) == []  # over the limit, no newline yet: dropped
    assert lines.feed(b"STATE failed\nok\n") == [b"ok"]  # its tail is not a line
    assert lines.feed(b"y" * 21 + b"\nfine\n") == [b"fine"]  # whole long line in one go
    assert lines.feed(b"z" * 20 + b"\n") == [b"z" * 20]  # exactly the limit is kept
    assert lines.feed(b"last") == [] and lines.end() == [b"last"]
    lines.feed(b"q" * 21)
    assert lines.end() == []


def test_qa18_runtime_limit_kill() -> None:
    import signal as sig

    assert meetings.runtime_limit_kill(-sig.SIGTERM, 700, 600)
    assert meetings.runtime_limit_kill(-sig.SIGKILL, 600, 600)
    assert not meetings.runtime_limit_kill(-sig.SIGKILL, 30, 600)  # e.g. out of memory
    assert not meetings.runtime_limit_kill(3, 700, 600)
    assert not meetings.runtime_limit_kill(0, 700, 600)
