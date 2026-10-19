#!/usr/bin/env python3
"""Copy files off the recorder's SD card over the USB console (no card reader needed).

Uses the firmware's `ls` and `get <file>` console commands: `get` sends the file as base64
lines between "@@BEGIN <name> <size>" and "@@END <crc32>". Each file is checked against its
size and CRC-32 before it is saved; files already in the output folder with the same size
are skipped.

    python firmware/recorder/tools/pull.py --port COM35 --out ~/barvis-recordings
    python firmware/recorder/tools/pull.py --port /dev/ttyACM0 --out rec rec_unset_8.wav

Keep the output folder outside the repository: recordings must never be committed.
Needs pyserial (it comes with ESP-IDF's Python environment).
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import time
import zlib
from pathlib import Path

import serial

LS_LINE = re.compile(r"^\s*(\d+)\s+(\S+)\s*$")
B64_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def open_port(port: str) -> serial.Serial:
    # Keep DTR/RTS low: on the ESP32-S3 USB Serial/JTAG, toggling them resets the chip.
    s = serial.Serial(None, 115200, timeout=0.5)
    s.port = port
    s.dtr = False
    s.rts = False
    s.open()
    return s


def command(s: serial.Serial, cmd: str, done: re.Pattern[str], timeout: float) -> list[str]:
    """Send a console command and collect output lines until one matches `done`.

    `timeout` is the longest silence allowed, so long transfers don't time out."""
    s.reset_input_buffer()
    s.write(cmd.encode() + b"\r\n")
    lines: list[str] = []
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = s.read(65536)
        if chunk:
            buf += chunk
            deadline = time.monotonic() + timeout
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").rstrip("\r")
            lines.append(line)
            if done.match(line):
                return lines
    raise TimeoutError(f"`{cmd}`: no reply within {timeout} s")


def list_card(s: serial.Serial) -> dict[str, int]:
    lines = command(s, "ls", re.compile(r"^\d+ file\(s\)$|^mount failed|^recording in progress"), 15)
    if not re.match(r"^\d+ file\(s\)$", lines[-1]):
        raise RuntimeError(f"ls: {lines[-1]}")
    return {m.group(2): int(m.group(1)) for m in map(LS_LINE.match, lines) if m}


def get_file(s: serial.Serial, name: str) -> bytes:
    lines = command(s, f"get {name}", re.compile(r"^@@END|^no such file|^usage|^mount failed|^recording in progress"), 15)
    begin = next((i for i, l in enumerate(lines) if l.startswith("@@BEGIN ")), None)
    if begin is None:
        raise RuntimeError(f"{name}: no @@BEGIN (device said: {' | '.join(lines[-3:])})")
    size = int(lines[begin].split()[2])
    end = lines[-1].split()
    # Log lines from other tasks can land between the data lines; they contain spaces or
    # colons, so only pure base64 lines are kept.
    data = base64.b64decode("".join(l for l in lines[begin + 1 : -1] if B64_LINE.match(l)))
    if len(end) > 2:
        raise RuntimeError(f"{name}: device reported {end[2]}")
    if len(data) != size:
        raise RuntimeError(f"{name}: got {len(data)} bytes, expected {size}")
    crc = zlib.crc32(data) & 0xFFFFFFFF
    if crc != int(end[1], 16):
        raise RuntimeError(f"{name}: CRC mismatch ({crc:08x} != {end[1]})")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", required=True, help="serial port, e.g. COM35 or /dev/ttyACM0")
    ap.add_argument("--out", required=True, type=Path, help="folder to save into (outside the repo)")
    ap.add_argument("files", nargs="*", help="file names (default: every .wav on the card)")
    args = ap.parse_args()

    out: Path = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    with open_port(args.port) as s:
        on_card = list_card(s)
        names = args.files or sorted(n for n in on_card if n.lower().endswith(".wav"))
        if not names:
            print("no .wav files on the card")
            return 0
        failed = 0
        for name in names:
            dest = out / name
            if name in on_card and dest.exists() and dest.stat().st_size == on_card[name]:
                print(f"skip  {name} (already here)")
                continue
            t0 = time.monotonic()
            try:
                data = get_file(s, name)
            except (RuntimeError, TimeoutError) as e:
                print(f"FAIL  {e}", file=sys.stderr)
                failed += 1
                continue
            dest.write_bytes(data)
            secs = time.monotonic() - t0
            print(f"ok    {name}: {len(data)} bytes in {secs:.1f} s -> {dest}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
