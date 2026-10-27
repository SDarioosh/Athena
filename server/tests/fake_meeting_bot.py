"""A stand-in for athena-meeting-bot (lane 1A) that speaks the launcher's contract.

Started by a small wrapper script the tests write, which sets FAKE_MODE (how to behave)
and FAKE_REPORT (where to write what it was given). It never joins anything.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time


def say(line: str) -> None:
    print(f"STATE {line}", flush=True)


def report(extra: dict | None = None) -> None:
    path = os.environ["FAKE_REPORT"]
    data = {"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid(),
            "pgid": os.getpgid(0), "nice": os.getpriority(os.PRIO_PROCESS, 0),
            "rlimit_as": resource.getrlimit(resource.RLIMIT_AS)[0], "cwd": os.getcwd(),
            **(extra or {})}
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def wait_for_term() -> None:
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    while not stop:
        time.sleep(0.02)


def main() -> int:
    mode = os.environ["FAKE_MODE"]
    time.sleep(0.2)  # the launcher caps the plain runner right after the start
    if mode == "failed":
        report()
        say("failed wrong_passcode")
        return 2
    if mode in ("owner", "noowner"):  # OBF: Zoom refuses until the owner is in
        say("starting waiting_for_owner")
        report()
        if mode == "noowner":
            time.sleep(0.3)
            say("failed owner_not_joined")
            return 2
        wait_for_term()
        say("ended stopped")
        return 0
    print("SDK: hello, this line is not a status line", flush=True)
    say("waiting_room")
    time.sleep(0.1)
    extra = {}
    if mode == "child":
        extra["child"] = subprocess.Popen(["sleep", "60"]).pid  # left behind on purpose
    say("recording no_audio" if mode == "noaudio" else "recording")
    report(extra)
    if mode in ("exit", "ownerleft"):
        time.sleep(0.3)
        say("ended meeting_ended" if mode == "exit" else "ended owner_left")
        return 0
    if mode == "crash":
        time.sleep(0.2)
        return 3
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
        return 0
    wait_for_term()  # record, noaudio, child: leave on SIGTERM
    say("ended stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
