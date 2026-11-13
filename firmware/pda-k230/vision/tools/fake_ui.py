#!/usr/bin/env python3
"""A stand-in for the UI's end of vision.sock, for testing athena-vision on the board.

Listens on the socket, prints every line athena-vision sends (with the time), and sends a
`mode` line for what you type: g = gestures, p = presence, q = QR, any combination
("gp"), or "off". Standard library only.

    python3 fake_ui.py [--socket /run/athena-pda/vision.sock] [--count-hands]
"""
import argparse
import json
import os
import select
import socket
import sys
import time


def mode_line(word):
    w = word.strip().lower()
    if w in ("off", "none", "0", ""):
        w = ""
    m = {"t": "mode", "gestures": "g" in w, "presence": "p" in w, "qr": "q" in w}
    return (json.dumps(m) + "\n").encode()


def main():
    ap = argparse.ArgumentParser()
    run = os.environ.get("ATHENA_PDA_RUN") or "/run/athena-pda"
    ap.add_argument("--socket", default=os.path.join(run, "vision.sock"))
    ap.add_argument("--count-hands", action="store_true", help="print hand lines per second")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.socket) or ".", exist_ok=True)
    try:
        os.unlink(a.socket)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(a.socket)
    os.chmod(a.socket, 0o600)
    srv.listen(1)
    print(f"listening on {a.socket}; type g/p/q/gp.../off and Enter", flush=True)
    conn, buf, t0, hands = None, b"", time.monotonic(), []
    try:
        while True:
            rd = [srv, sys.stdin] + ([conn] if conn else [])
            ready, _, _ = select.select(rd, [], [], 1.0)
            now = time.monotonic() - t0
            if srv in ready:
                if conn:
                    conn.close()
                conn, _ = srv.accept()
                buf = b""
                print(f"{now:8.3f} connected", flush=True)
            if conn and conn in ready:
                data = conn.recv(65536)
                if not data:
                    print(f"{now:8.3f} disconnected", flush=True)
                    conn.close()
                    conn = None
                else:
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace")
                        print(f"{now:8.3f} {text}", flush=True)
                        if a.count_hands and '"t":"hand"' in text:
                            hands = [t for t in hands if t > now - 1.0] + [now]
                            print(f"{now:8.3f} # hand lines in the last second: {len(hands)}")
            if sys.stdin in ready:
                word = sys.stdin.readline()
                if not word:
                    break
                if conn:
                    conn.sendall(mode_line(word))
                    print(f"{now:8.3f} > {mode_line(word).decode().strip()}", flush=True)
                else:
                    print("not connected yet", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if conn:
            conn.close()
        srv.close()
        try:
            os.unlink(a.socket)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
