#!/usr/bin/env python3
"""Drive the recorder's USB console without `idf.py monitor` (for scripts and agents).

    python firmware/recorder/tools/wcon.py --port /dev/cu.usbmodem101 status
    python firmware/recorder/tools/wcon.py --port COM35 "session start"
    python firmware/recorder/tools/wcon.py --port COM35 --boot          # reset, print boot log
    python firmware/recorder/tools/wcon.py --port COM35 --listen 30     # print logs for 30 s

A command is sent once; output is read until the `recorder>` prompt comes back (after at
least `--min` seconds, so log lines from a slow command are kept) or `--timeout` seconds of
silence. `--boot` resets the chip over the USB Serial/JTAG (RTS pulse, as esptool does),
reopens the port after it re-enumerates and prints the log until the prompt shows up.

The console and the logs share the port, and only one program can hold it at a time.
Needs pyserial (it comes with ESP-IDF's Python environment).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

PROMPT = b"recorder>"


def strip_echo(text: str, cmd: str) -> str:
    """Drop the echoed command line and the trailing prompt from console output."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in lines:
        bare = line.replace(PROMPT.decode(), "").strip()
        if not out and cmd and bare == cmd:
            continue
        if line.strip() == PROMPT.decode() or (PROMPT.decode() in line and bare in ("", cmd)):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


def has_prompt(buf: bytes, cmd: str | None = None) -> bool:
    """True if the console printed its prompt at the end of `buf` (ignoring spaces).

    With `cmd`, also True if the prompt came back earlier and log lines followed it (ESP-IDF
    logs are asynchronous): any prompt on a finished line other than the echo of `cmd`."""
    if buf.rstrip().endswith(PROMPT):
        return True
    if not cmd:
        return False
    lines = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    for line in lines[:-1]:  # the last one isn't finished yet (it may be the echo)
        at = line.find(PROMPT)
        if at >= 0 and line[at + len(PROMPT):].strip() != cmd.encode():
            return True
    return False


def _is_device_path(port: str) -> bool:
    """A port that is a file, so its absence means it went away: POSIX `/dev/...`, not
    Windows `COM35` (never a path, so os.path.exists() is always False for it)."""
    return os.name == "posix" and port.startswith("/dev/")


def open_port(port: str, wait: float = 15.0):
    """Open the port, retrying while it re-enumerates, without resetting the chip.

    The ESP32-S3 USB Serial/JTAG resets the chip when RTS is high while DTR is low. The OS
    raises both on open, and pyserial would lower DTR first (a reset), so open with both
    left as the OS sets them, then lower RTS before DTR."""
    import serial

    deadline = time.monotonic() + wait
    while True:
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
            s.rts = False
            s.dtr = False
            return s
        except (serial.SerialException, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def read_until_prompt(s, timeout: float, min_time: float = 0.0, echo=None,
                      cmd: str | None = None) -> bytes:
    buf = b""
    start = time.monotonic()
    last = start
    while True:
        try:
            chunk = s.read(4096)
        except OSError:  # port went away (reset): stop here
            break
        now = time.monotonic()
        if chunk:
            buf += chunk
            last = now
            if echo:
                echo(chunk)
        if has_prompt(buf, cmd) and now - start >= min_time:
            break
        if now - last > timeout:
            break
    return buf


def reset(port: str) -> None:
    s = open_port(port)
    s.rts = True
    time.sleep(0.1)
    s.rts = False
    try:
        s.close()
    except OSError:
        pass
    time.sleep(0.5)


def write_out(chunk: bytes) -> None:
    sys.stdout.write(chunk.decode("utf-8", "replace"))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True)
    p.add_argument("--timeout", type=float, default=5.0, help="seconds of silence before giving up")
    p.add_argument("--min", type=float, default=0.0, help="read at least this long")
    p.add_argument("--boot", action="store_true", help="reset and print the boot log")
    p.add_argument("--listen", type=float, help="just print the log for N seconds")
    p.add_argument("command", nargs="*")
    a = p.parse_args(argv)

    if a.boot:
        reset(a.port)
        s = open_port(a.port)
        read_until_prompt(s, timeout=a.timeout + 10, min_time=a.min, echo=write_out)
        # The REPL prints its prompt before the last init logs; give them a moment.
        read_until_prompt(s, timeout=1.0, echo=write_out)
        print()
        s.close()
        if not a.command:
            return 0

    s = open_port(a.port)
    if a.listen is not None:
        # Follows the watch through a reset or power-off: when the port goes away, wait for
        # it to come back and go on, so the next boot log is kept.
        end = time.monotonic() + a.listen
        while time.monotonic() < end:
            try:
                chunk = s.read(4096)
            except OSError:
                chunk, gone = b"", True
            else:
                gone = not chunk and _is_device_path(a.port) and not os.path.exists(a.port)
            if chunk:
                write_out(chunk)
            if gone:
                write_out(b"\n[wcon: port gone, waiting for it]\n")
                try:
                    s.close()
                except OSError:
                    pass
                while time.monotonic() < end:
                    try:
                        s = open_port(a.port, wait=1.0)
                        break
                    except OSError:
                        pass
                write_out(b"[wcon: port back]\n")
        s.close()
        return 0

    cmd = " ".join(a.command)
    if not cmd:
        p.error("give a command, --boot or --listen")
    s.reset_input_buffer()
    s.write(cmd.encode() + b"\r\n")
    buf = read_until_prompt(s, timeout=a.timeout, min_time=a.min, cmd=cmd)
    s.close()
    print(strip_echo(buf.decode("utf-8", "replace"), cmd))
    return 0 if has_prompt(buf, cmd) else 1


if __name__ == "__main__":
    sys.exit(main())
