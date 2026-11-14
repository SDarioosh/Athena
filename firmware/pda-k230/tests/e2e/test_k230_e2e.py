"""The K230 PDA end to end on a PC: the real UI and the real syncd against the fake hub, the
UI driven like a finger through its headless test control socket, a scripted vision client
on vision.sock. See k230_e2e_rig.py. Skipped unless ATHENA_PDA_UI_BIN names the UI binary.

    cmake -S firmware/pda-k230/ui -B <build> && cmake --build <build> --target athena-pda
    ATHENA_PDA_UI_BIN=<build>/athena-pda python -m pytest -q firmware/pda-k230/tests/e2e
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import subprocess
import time
from pathlib import Path

import pytest
from k230_e2e_rig import UI_BIN, VISION_TOOLS, Rig, wait_for

pytestmark = pytest.mark.skipif(not UI_BIN or not os.access(UI_BIN, os.X_OK),
                                reason="ATHENA_PDA_UI_BIN (the athena-pda binary) not set")

TAB_TODAY, TAB_BRIEF, TAB_TASKS, TAB_TALKS, TAB_STATUS = 0, 1, 3, 4, 5


@pytest.fixture
def rig():
    out = os.environ.get("E2E_OUT_DIR")  # keep the screenshots there
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
    r = Rig(Path(out) if out else None)
    yield r
    r.close()
    r.cleanup()


def _pair_by_scan(rig: Rig) -> None:
    ctl = rig.ctl
    vision = rig.connect_vision()
    vision.send({"t": "state", "camera": "off", "fps": 0, "msg": ""})
    ctl.cmd(f"tab {TAB_STATUS}")
    wait_for(lambda: ctl.has("Not paired"), 15, "Status to say Not paired (syncd's net)")
    ctl.click("Pair with Odysseus")
    assert ctl.state()["detail"] == 1
    ctl.click("Scan the QR code")
    wait_for(lambda: (vision.mode() or {}).get("qr") is True, 5, "mode qr on")
    vision.send({"t": "state", "camera": "on", "fps": 15, "msg": ""})
    ctl.cmd("wait 200")
    vision.send({"t": "qr", "text": "https://example.com/not-a-pairing-code"})
    ctl.cmd("wait 300")
    assert ctl.has("isn't an Odysseus pairing code")  # the UI rejects it itself, scanning goes on
    vision.send({"t": "qr", "text": rig.pair_link()})
    wait_for(lambda: ctl.has("Paired with"), 15, "pair_result ok on screen")
    wait_for(lambda: (vision.mode() or {}).get("qr") is False, 5, "mode qr off again")
    assert (rig.data / "token").stat().st_mode & 0o777 == 0o600


def test_pair_sync_tick_fetch_revoke_restart(rig: Rig) -> None:
    rig.start_ui()
    rig.start_syncd()
    ctl = rig.ctl
    try:
        # ---- pairing by the camera's QR code
        _pair_by_scan(rig)
        assert "/api/pair/redeem" in rig.hub.paths("POST")
        ctl.cmd("wait 400")  # the result fades in
        rig.shot("e2e_01_paired")
        ctl.click("Done")

        # ---- the first sync puts the feed into the UI's store
        feed = wait_for(rig.feed, 30, "feed.json")
        assert [c["id"] for c in feed["conversations"]] == ["c_new", "c_old"]
        assert feed["todos"][0]["items"][0]["text"] == "Milk"
        assert rig.hub.paths()[1] == "/api/codex/athena/health"
        ctl.cmd(f"tab {TAB_STATUS}")
        wait_for(lambda: ctl.has("Synced"), 15, "Status: synced")
        rig.shot("e2e_02_status_synced")

        # ---- a to-do tick queued in the UI reaches the hub and is acked
        ctl.cmd(f"tab {TAB_TASKS}")
        assert not rig.outbox()
        ctl.click("Milk")  # syncd hears the STATUS notify and syncs at once
        wait_for(lambda: '"t":"todo_toggle","note":"n-groceries","i":0' in rig.ui_log(), 5,
                 "the UI queued the tick")
        wait_for(lambda: rig.hub.ody.notes[0]["items"][0]["done"], 30, "Milk done in the hub")
        wait_for(lambda: ctl.state()["outbox"] == 0, 15, "the tick acked")
        assert not rig.outbox()
        rig.shot("e2e_03_tasks_ticked")

        # ---- a conversation, then its transcript: fetched by a `fetch` action, then cached
        ctl.cmd(f"tab {TAB_TALKS}")
        ctl.click("Standup")
        assert ctl.state()["detail"] == 1
        rig.shot("e2e_04_conversation")
        ctl.click("Read the transcript")
        wait_for(lambda: '"t":"fetch","ref":"transcript:c_new' in rig.ui_log(), 5,
                 "the UI queued the fetch")
        wait_for(rig.details, 30, "a detail blob in d/")
        wait_for(lambda: ctl.state()["outbox"] == 0, 15, "the fetch acked")
        ctl.cmd("wait 500")
        rig.shot("e2e_05_transcript_fetched")
        ctl.cmd("tab 0")  # leaves the detail

        # ---- the hub revokes the token
        rig.hub.revoked = True
        ctl.cmd(f"tab {TAB_STATUS}")
        ctl.click("Sync now")
        wait_for(lambda: ctl.has("Pairing lost"), 30, "auth_failed on Status")
        assert "auth_failed" in rig.syncd_log()
        rig.shot("e2e_06_auth_failed")

        # ---- the UI restarts: syncd reconnects and syncs at once
        rig.hub.revoked = False
        n = len(rig.hub.requests)
        rig.stop_ui()
        rig.start_ui()
        ctl = rig.ctl
        ctl.cmd(f"tab {TAB_STATUS}")
        wait_for(lambda: len(rig.hub.requests) > n, 30, "a sync after the restart")
        wait_for(lambda: ctl.has("Synced"), 30, "Status: synced after the restart")
        assert rig.syncd.poll() is None  # syncd survived the UI going away
    except BaseException:
        print(rig.dump_logs())
        raise


def _frame(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack("<BH", kind, len(payload)) + payload


def _read_frame(s: socket.socket) -> tuple[int, bytes]:
    head = b""
    while len(head) < 3:
        chunk = s.recv(3 - len(head))
        if not chunk:
            raise ConnectionError("closed")
        head += chunk
    kind, n = struct.unpack("<BH", head)
    body = b""
    while len(body) < n:
        body += s.recv(n - len(body))
    return kind, body


def test_glance_contract_basics(rig: Rig) -> None:
    """Contract 1 as the UI implements it: both sockets 0600, INFO's text, a 16-byte STATUS,
    one client at a time (a second is closed), and the link state follows the client."""
    rig.start_ui()
    glance = rig.run / "glance.sock"
    assert stat.S_IMODE(glance.stat().st_mode) == 0o600
    assert stat.S_IMODE((rig.run / "vision.sock").stat().st_mode) == 0o600
    a = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    a.settimeout(5)
    a.connect(str(glance))
    a.sendall(_frame(0x05))
    assert _read_frame(a) == (0x06, b"proto=1;device=k230-pda;fw=0.1.0;board=t_display_k230")
    a.sendall(_frame(0x03))
    kind, status = _read_frame(a)
    assert kind == 0x04 and len(status) == 16
    b = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    b.settimeout(5)
    b.connect(str(glance))
    b.sendall(_frame(0x05))
    with pytest.raises((ConnectionError, OSError)):
        _read_frame(b)  # refused: closed without an answer
    b.close()
    a.sendall(_frame(0x05))  # the first one still works
    assert _read_frame(a)[0] == 0x06
    a.close()


def test_vision_gestures(rig: Rig) -> None:
    """Hands-free on (the Status switch): vision is told gestures, a swipe moves tabs, palm
    goes back to Today, fist shows the ambient screen."""
    rig.start_ui()
    ctl = rig.ctl
    try:
        vision = rig.connect_vision()
        assert vision.mode() == {"t": "mode", "gestures": False, "presence": False, "qr": False}
        ctl.cmd(f"tab {TAB_STATUS}")
        ctl.click("Hands-free")
        wait_for(lambda: (vision.mode() or {}).get("gestures") is True, 5, "mode gestures on")
        settings = json.loads((rig.data / "ui.json").read_text())
        assert settings["hands_free"] is True
        vision.send({"t": "state", "camera": "on", "fps": 15, "msg": ""})
        ctl.cmd(f"tab {TAB_TODAY}")
        vision.send({"t": "gesture", "g": "swipe_left", "conf": 0.9})
        wait_for(lambda: ctl.state()["tab"] == TAB_BRIEF, 5, "swipe_left: the next tab")
        vision.send({"t": "gesture", "g": "palm", "conf": 0.9})
        wait_for(lambda: ctl.state()["tab"] == TAB_TODAY, 5, "palm: Today")
        vision.send({"t": "gesture", "g": "fist", "conf": 0.9})
        wait_for(lambda: ctl.state()["off"] == 1, 5, "fist: the ambient screen")
        rig.shot("e2e_07_ambient_by_fist")
        vision.send({"t": "gesture", "g": "palm", "conf": 0.9})
        wait_for(lambda: ctl.state()["off"] == 0, 5, "palm wakes it")
    except BaseException:
        print(rig.dump_logs())
        raise


@pytest.mark.skipif(not VISION_TOOLS, reason="ATHENA_VISION_TOOLS (a vision build) not set")
def test_vision_replay_into_ui(rig: Rig) -> None:
    """The vision core's own output (a synthetic swipe through athena-vision-replay) sent to
    the UI line by line, as athena-vision would."""
    tools = Path(VISION_TOOLS)
    synth = subprocess.run([str(tools / "synth_track"), "swipe_left", "--fps", "15"],
                           check=True, capture_output=True)
    replay = subprocess.run([str(tools / "athena-vision-replay"), "--raw", "-"],
                            input=synth.stdout, check=True, capture_output=True)
    lines = [ln for ln in replay.stdout.decode().splitlines() if ln.startswith("{")]
    assert any('"swipe_left"' in ln for ln in lines)
    rig.start_ui()
    ctl = rig.ctl
    try:
        vision = rig.connect_vision()
        ctl.cmd(f"tab {TAB_STATUS}")
        ctl.click("Hands-free")
        wait_for(lambda: (vision.mode() or {}).get("gestures") is True, 5, "mode gestures on")
        ctl.cmd(f"tab {TAB_TODAY}")
        for ln in lines:
            vision.send(ln)
            time.sleep(1 / 15)
        wait_for(lambda: ctl.state()["tab"] == TAB_BRIEF, 5, "the replayed swipe moved a tab")
    except BaseException:
        print(rig.dump_logs())
        raise
