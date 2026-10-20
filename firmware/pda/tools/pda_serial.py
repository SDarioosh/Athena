#!/usr/bin/env python3
"""USB console helper for the PDA firmware (firmware/pda/README.md).

  pda_serial.py -p COM36 log [--seconds 10]          print the console output
  pda_serial.py -p COM36 cmd "batt" "rtc"            run commands, print their output
  pda_serial.py -p COM36 put feed.json [--kind 1]    load a blob over the console
  pda_serial.py -p COM36 fb screen.png               dump the framebuffer as a PNG

Needs pyserial. Fictional data only: never load real feeds through this on a shared PC.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import struct
import sys
import time
import zlib

import serial


def open_port(name: str) -> serial.Serial:
    s = serial.Serial()
    s.port = name
    s.baudrate = 115200
    s.timeout = 0.1
    # USB-Serial/JTAG: don't toggle DTR/RTS on open, that resets the chip.
    s.dtr = False
    s.rts = False
    s.open()
    return s


def read_for(s: serial.Serial, seconds: float, until: bytes | None = None) -> bytes:
    out = bytearray()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        chunk = s.read(4096)
        if chunk:
            out += chunk
            if until and until in out:
                break
    return bytes(out)


def run_cmd(s: serial.Serial, line: str, wait: float = 2.0, until: bytes = b"pda> ") -> str:
    """Send one command; return its output once the next prompt shows (or after `wait` s)."""
    s.reset_input_buffer()
    s.write(line.encode() + b"\r\n")
    return read_for(s, wait, until=until).decode("utf-8", "replace")


def cmd_log(a) -> int:
    with open_port(a.port) as s:
        sys.stdout.write(read_for(s, a.seconds).decode("utf-8", "replace"))
    return 0


def cmd_cmd(a) -> int:
    with open_port(a.port) as s:
        read_for(s, 0.3)
        for line in a.lines:
            sys.stdout.write(run_cmd(s, line, a.wait))
            sys.stdout.write("\n")
    return 0


def cmd_put(a) -> int:
    with open(a.file, "rb") as f:
        data = f.read()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    with open_port(a.port) as s:
        read_for(s, 0.3)
        r = run_cmd(s, f"put begin {a.kind} {len(data)} {crc:08x}", 3)
        if "ready" not in r:
            sys.stdout.write(r)
            return 1
        for off in range(0, len(data), 120):
            b64 = base64.b64encode(data[off : off + 120]).decode()
            r = run_cmd(s, f"put data {b64}", 3)
            if "ERR" in r:
                sys.stdout.write(r)
                return 1
        sys.stdout.write(run_cmd(s, "put end", 8))
    return 0


def png_gray4(path: str, w: int, h: int, fb: bytes, rot: int = 0) -> None:
    """Write the 4-bit panel buffer as an 8-bit grey PNG, turned upright for `rot`."""
    px = [[0] * w for _ in range(h)]
    for y in range(h):
        line = fb[y * w // 2 : (y + 1) * w // 2]
        row = px[y]
        for i, b in enumerate(line):
            row[2 * i] = (b & 0x0F) * 17  # the lower nibble is the left pixel in epdiy
            row[2 * i + 1] = (b >> 4) * 17
    if rot == 270:  # EPD_ROT_INVERTED_PORTRAIT: screen (x, y) = panel (y, h - 1 - x)
        px = [[px[h - 1 - x][y] for x in range(h)] for y in range(w)]
    elif rot == 90:  # EPD_ROT_PORTRAIT: screen (x, y) = panel (w - 1 - y, x)
        px = [[px[x][w - 1 - y] for x in range(h)] for y in range(w)]
    out_h, out_w = len(px), len(px[0])
    rows = bytearray()
    for r in px:
        rows.append(0)
        rows += bytes(r)

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = binascii.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", out_w, out_h, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


B64_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def cmd_fb(a) -> int:
    for attempt in range(3):
        with open_port(a.port) as s:
            read_for(s, 0.3)
            s.reset_input_buffer()
            s.write(b"fb\r\n")
            raw = read_for(s, 30, until=b"FB END")
        text = raw.decode("ascii", "replace")
        _, _, rest = text.partition("FB BEGIN ")
        if not rest:
            continue
        head = rest.split("\n", 1)[0].split()
        w, h, crc = head[:3]
        rot = int(head[3]) if len(head) > 3 else 0
        body = rest.split("\n", 1)[1].split("FB END", 1)[0]
        # Log lines from other tasks can land between the data lines: skip them.
        lines = [x.strip() for x in body.splitlines() if B64_LINE.match(x.strip())]
        fb = b"".join(base64.b64decode(x) for x in lines)
        if zlib.crc32(fb) & 0xFFFFFFFF == int(crc, 16):
            png_gray4(a.out, int(w), int(h), fb, rot)
            print(f"wrote {a.out} (panel {w}x{h}, shown upright for rotation {rot})")
            return 0
        print(f"framebuffer CRC mismatch (attempt {attempt + 1})", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-p", "--port", required=True)
    sub = p.add_subparsers(dest="what", required=True)
    lg = sub.add_parser("log")
    lg.add_argument("--seconds", type=float, default=10)
    lg.set_defaults(fn=cmd_log)
    c = sub.add_parser("cmd")
    c.add_argument("lines", nargs="+")
    c.add_argument("--wait", type=float, default=3)
    c.set_defaults(fn=cmd_cmd)
    pu = sub.add_parser("put")
    pu.add_argument("file")
    pu.add_argument("--kind", type=int, default=1)
    pu.set_defaults(fn=cmd_put)
    f = sub.add_parser("fb")
    f.add_argument("out")
    f.set_defaults(fn=cmd_fb)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
