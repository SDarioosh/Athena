"""The K230 PDA's processes run together on a PC: the real UI (`athena-pda`, headless, with
its test control socket), the real syncd (`python -m athena_pda_sync`) as a subprocess, the
syncd tests' fake Odysseus hub (`syncd_fakes.FakeHub`, in this process) and a scripted
`vision.sock` client standing in for athena-vision. Fictional data only.

Environment: `ATHENA_PDA_UI_BIN` the UI binary (tests skip without it);
`ATHENA_VISION_TOOLS` a vision build directory with `synth_track` and
`athena-vision-replay` (the replay test skips without it)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

K230 = Path(__file__).resolve().parents[2]
SYNCD = K230 / "syncd"
sys.path.insert(0, str(SYNCD / "tests"))

import _syncd_path  # noqa: E402,F401 (syncd, athena_relay and the relay's fakes on the path)
from syncd_fakes import CODE, HUB_TOKEN, FakeHub  # noqa: E402

__all__ = ["CODE", "HUB_TOKEN", "FakeHub", "NOW", "Rig", "UI_BIN", "VISION_TOOLS", "wait_for"]

UI_BIN = os.environ.get("ATHENA_PDA_UI_BIN", "")
VISION_TOOLS = os.environ.get("ATHENA_VISION_TOOLS", "")
NOW = 1_792_486_800  # 2026-10-20T09:00:00Z, the fakes' demo clock


def wait_for(pred, timeout: float = 20.0, what: str = "a condition", step: float = 0.05) -> Any:
    end = time.monotonic() + timeout
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(step)


class Ctl:
    """The UI's test control socket (ui/src/platform/headless_ctl.h)."""

    def __init__(self, path: Path) -> None:
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(10)
        self.s.connect(str(path))
        self.buf = b""

    def cmd(self, line: str) -> str:
        self.s.sendall(line.encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("the UI closed the control socket")
            self.buf += chunk
        out, self.buf = self.buf.split(b"\n", 1)
        answer = out.decode()
        if not answer.startswith("ok"):
            raise AssertionError(f"{line!r}: {answer}")
        return answer[3:]

    def has(self, text: str) -> bool:
        return self.cmd(f"has {text}") == "yes"

    def click(self, text: str) -> None:
        self.cmd(f"click {text}")
        self.cmd("wait 400")  # the screens rebuild after the event and animate

    def state(self) -> dict[str, int]:
        return {k: int(v) for k, v in (p.split("=") for p in self.cmd("state").split())}

    def close(self) -> None:
        self.s.close()


class Vision:
    """athena-vision's end of vision.sock: reads `mode`, sends what the test says."""

    def __init__(self, path: Path) -> None:
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(str(path))
        self.s.setblocking(False)
        self.buf = b""
        self.modes: list[dict[str, Any]] = []

    def poll(self) -> list[dict[str, Any]]:
        with contextlib.suppress(BlockingIOError):
            while True:
                chunk = self.s.recv(4096)
                if not chunk:
                    break
                self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg.get("t") == "mode":
                self.modes.append(msg)
        return self.modes

    def mode(self) -> dict[str, Any] | None:
        modes = self.poll()
        return modes[-1] if modes else None

    def send(self, msg: dict[str, Any] | str) -> None:
        line = msg if isinstance(msg, str) else json.dumps(msg)
        self.s.setblocking(True)
        self.s.sendall(line.encode() + b"\n")
        self.s.setblocking(False)

    def close(self) -> None:
        self.s.close()


class Rig:
    def __init__(self, out: Path | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="k230e2e-"))  # short: APP_DIR_MAX, sun_path
        self.data = self.tmp / "d"
        self.run = self.tmp / "r"
        self.data.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        self.out = out or self.tmp
        self.hub = FakeHub()
        self.ui: subprocess.Popen | None = None
        self.syncd: subprocess.Popen | None = None
        self.ctl: Ctl | None = None
        self.vision: Vision | None = None
        self.logs: dict[str, Path] = {}
        (self.data / "config.json").write_text(json.dumps(
            {"tz": "Europe/Berlin", "tailscale": False}))

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        env.update(ATHENA_PDA_DIR=str(self.data), ATHENA_PDA_RUN=str(self.run))
        # the fake hub is on 127.0.0.1, which syncd refuses unless told (addresses.py)
        env["ATHENA_PDA_DEV_LOOPBACK_HUB"] = "1"
        return env

    def _log(self, name: str):
        n = sum(1 for k in self.logs if k.startswith(name))
        path = self.tmp / f"{name}{n}.log"
        self.logs[f"{name}{n}"] = path
        return path.open("w")

    # ---------------------------------------------------------------- the UI

    def start_ui(self) -> None:
        env = self.env()
        env.update(ATHENA_PDA_BACKEND="headless", ATHENA_PDA_TEST_CTL=str(self.run / "ctl.sock"))
        with self._log("ui") as log:
            self.ui = subprocess.Popen([UI_BIN], env=env, stdout=log, stderr=subprocess.STDOUT)
        wait_for(lambda: (self.run / "ctl.sock").exists() and (self.run / "vision.sock").exists(),
                 10, "the UI's sockets")
        self.ctl = Ctl(self.run / "ctl.sock")
        self.ctl.cmd(f"clock {NOW}")  # the fixtures' day, so Today and Talks show them

    def stop_ui(self) -> None:
        if self.vision:
            self.vision.close()
            self.vision = None
        if self.ctl:
            self.ctl.close()
            self.ctl = None
        if self.ui and self.ui.poll() is None:
            self.ui.send_signal(signal.SIGTERM)
            self.ui.wait(10)
        self.ui = None

    def connect_vision(self) -> Vision:
        self.vision = Vision(self.run / "vision.sock")
        wait_for(self.vision.mode, 5, "the UI's first mode line")
        return self.vision

    # ---------------------------------------------------------------- syncd

    def start_syncd(self) -> None:
        env = self.env()
        env["PYTHONPATH"] = str(SYNCD)
        with self._log("syncd") as log:
            self.syncd = subprocess.Popen([sys.executable, "-m", "athena_pda_sync"], env=env,
                                          stdout=log, stderr=subprocess.STDOUT)

    def ui_log(self) -> str:
        return "".join(p.read_text() for k, p in self.logs.items() if k.startswith("ui"))

    def syncd_log(self) -> str:
        return "".join(p.read_text() for k, p in self.logs.items() if k.startswith("syncd"))

    # ---------------------------------------------------------------- state on disk

    def feed(self) -> dict[str, Any] | None:
        with contextlib.suppress(FileNotFoundError, ValueError):
            return json.loads((self.data / "feed.json").read_text())
        return None

    def outbox(self) -> list[str]:
        ob = self.data / "ob"
        return sorted(p.name for p in ob.iterdir() if p.name != "next") if ob.is_dir() else []

    def details(self) -> list[str]:
        d = self.data / "d"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def shot(self, name: str) -> Path:
        path = self.out / f"{name}.png"
        assert self.ctl is not None
        self.ctl.cmd(f"png {path}")
        return path

    def pair_link(self) -> str:
        return f"odysseus-pair:v1?url={self.hub.url}&code={CODE}"

    def close(self) -> None:
        self.stop_ui()
        if self.syncd and self.syncd.poll() is None:
            self.syncd.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.syncd.wait(10)
            if self.syncd.poll() is None:
                self.syncd.kill()
        self.hub.close()

    def dump_logs(self) -> str:
        return "\n".join(f"--- {k}\n{p.read_text()[-4000:]}" for k, p in self.logs.items()
                         if p.exists())

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
