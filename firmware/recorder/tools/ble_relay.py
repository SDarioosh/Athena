#!/usr/bin/env python3
"""PC stand-in for the phone relay: drain the T-Watch Ultra over BLE into the server.

Speaks docs/twatch-ble-protocol.md (v1) with bleak: scan for the watch's service, send TIME,
check the server's /health, then LIST / READ / upload / ACK every queued chunk and BYE. A
chunk is deleted on the watch only after the server answered ok (and /end, for a session's
last chunk). The audio goes straight from the watch to your server; nothing touches the
cloud, the token is never logged.

Setup (once; Python 3.10+, a Bluetooth LE adapter):

    python3 -m venv ~/.venvs/barvis-relay
    ~/.venvs/barvis-relay/bin/pip install -r firmware/recorder/tools/requirements.txt
    # Windows: py -m venv %USERPROFILE%\\venvs\\barvis-relay, then its Scripts\\pip

The token comes from the CONVO_INGEST_TOKEN environment variable or --token-file (a file
holding only the token); there is deliberately no --token option, so it can't end up in your
shell history. The server URL comes from --server or BARVIS_SERVER.

First pairing. The watch accepts new bonds only in its 2-minute sync window (hold BOOT, or
`ble pair` on the console) and shows a 6-digit code on its screen, which you type on the PC.
bleak can't type it, so the operating system pairs:

    # Linux: bluetoothctl -> `agent on`, `default-agent`, `scan on`, `pair <address>`, type the
    #   code. (Or keep bluetoothctl open with its agent and run --pair below: BlueZ asks
    #   for the code in the bluetoothctl window.)
    # Windows: Settings > Bluetooth & devices > Add device > Bluetooth > "barvis-tw", type the code
    # macOS: run --list below; macOS asks for the code when the relay first connects

Then look at the queue without changing anything:

    python firmware/recorder/tools/ble_relay.py --list

Drain once and exit (exit code 0 = all delivered):

    export BARVIS_SERVER=http://<server>:8000 CONVO_INGEST_TOKEN=...   # or --token-file
    python firmware/recorder/tools/ble_relay.py --once

Keep running (scan, drain whenever the watch advertises, back off 1/2/5/10/15 min while the
server is unreachable):

    python firmware/recorder/tools/ble_relay.py

Check transfers without uploading or deleting anything (saves verified chunks to a folder
outside the repository):

    python firmware/recorder/tools/ble_relay.py --dry-run --save ~/barvis-chunks --once

The T5 E-Paper Pro PDA has its own subcommand (see pda_relay.py):

    python firmware/recorder/tools/ble_relay.py pda --help

Exit codes for --once/--list: 0 ok, 1 some chunks skipped, 2 retry later (server, link or
watch error), 3 token rejected / incompatible / bad settings, 4 watch not found.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import stat
import sys
from pathlib import Path

from barvis_relay import drain as d
from barvis_relay import protocol as p
from barvis_relay.anchors import Anchors, default_state_dir
from barvis_relay.uploader import Uploader

log = logging.getLogger("barvis_relay.ble")
REPO_ROOT = Path(__file__).resolve().parents[3]
LINK_RETRY_SECONDS = 5
SCAN_ERROR_RETRY_SECONDS = 30
NOT_FOUND = 4
# --once/--list: connections that drop before the watch's info is read are tried this often
# (Windows sometimes reports a connection the watch never saw, then closes it at once).
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_SECONDS = 2


class BleakTransport:
    """drain.Transport over a connected bleak client: `data` notifications -> a queue."""

    def __init__(self, client, queue: asyncio.Queue[bytes | None]) -> None:
        self.client = client
        self.queue = queue

    async def open(self) -> None:
        def on_data(_char, data: bytearray) -> None:
            self.queue.put_nowait(bytes(data))

        def on_status(_char, data: bytearray) -> None:
            try:
                log.debug("status: %s", p.Status.decode(bytes(data)).describe())
            except p.ProtocolError as e:
                log.debug("status notification: %s", e)

        # Both need an encrypted link: a new (in-window) relay pairs here (§3.1, §6 step 3).
        await self._gatt(self.client.start_notify(p.DATA_UUID, on_data))
        await self._gatt(self.client.start_notify(p.STATUS_UUID, on_status))

    async def _gatt(self, aw):
        try:
            from bleak.exc import BleakError
        except ImportError:  # only in tests without bleak
            BleakError = OSError  # noqa: N806
        try:
            return await aw
        except (BleakError, OSError, EOFError) as e:
            raise d.LinkLost(f"{type(e).__name__}: {e}") from None

    async def read_info(self) -> bytes:
        return bytes(await self._gatt(self.client.read_gatt_char(p.INFO_UUID)))

    async def read_status(self) -> bytes:
        return bytes(await self._gatt(self.client.read_gatt_char(p.STATUS_UUID)))

    async def write(self, command: bytes) -> None:
        if not self.client.is_connected:
            raise d.LinkLost("disconnected")
        await self._gatt(self.client.write_gatt_char(p.CTRL_UUID, command, response=True))

    async def receive(self, timeout: float) -> bytes:
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if item is None:
            raise d.LinkLost("the watch disconnected")
        return item

    def discard_pending(self) -> int:
        n = 0
        while not self.queue.empty():
            if self.queue.get_nowait() is None:
                self.queue.put_nowait(None)  # keep the disconnect marker
                break
            n += 1
        return n


async def find_watch(address: str | None, timeout: float):
    from bleak import BleakScanner

    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def match(_dev, adv) -> bool:
        return p.SERVICE_UUID in [u.lower() for u in adv.service_uuids]

    return await BleakScanner.find_device_by_filter(match, timeout=timeout,
                                                    service_uuids=[p.SERVICE_UUID])


async def connect_and_run(relay: d.Relay, device, *, pair: bool) -> d.Report:
    from bleak import BleakClient
    from bleak.exc import BleakError

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_disconnect(_client) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, None)

    client = BleakClient(device, disconnected_callback=on_disconnect, timeout=20)
    try:
        await client.connect()
    except (BleakError, OSError, asyncio.TimeoutError, TimeoutError) as e:  # noqa: UP041
        return d.Report(outcome=d.LINK_LOST, message=f"connect failed: {type(e).__name__}: {e}")
    try:
        log.info("connected to %s", client.address)
        if pair:
            try:
                await client.pair()
                log.info("paired (bonded) with %s", client.address)
            except NotImplementedError:
                log.info("this OS pairs on first encrypted access; accept the system prompt")
            except BleakError as e:
                log.error("pairing failed: %s. Open the watch's sync window and pair through the "
                          "OS so you can type the code from the watch screen (see --help)", e)
        backend = str(getattr(client, "backend_id", ""))
        if "bluez" not in backend.lower():  # BlueZ always reports 23 here
            with contextlib.suppress(Exception):
                log.debug("ATT MTU %d", client.mtu_size)
        t = BleakTransport(client, queue)
        try:
            await t.open()
        except d.LinkLost as e:
            return d.Report(outcome=d.LINK_LOST,
                            message=f"subscribe failed ({e}); not paired? Open the watch's "
                                    "sync window and pair through the OS (see the docstring)")
        return await relay.run(t)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def connect_again(report: d.Report, attempt: int) -> bool:
    """Whether a one-shot run connects again: the link was lost before it carried anything."""
    return report.outcome == d.LINK_LOST and report.info is None and attempt < CONNECT_ATTEMPTS


def print_list(report: d.Report) -> None:
    if report.info:
        i = report.info
        print(f"watch   device={i.device} fw={i.fw or '?'} board={i.board or '?'} "
              f"proto={i.proto} last reset={i.extra.get('reset', '?')}")
    if report.status:
        print(f"status  {report.status.describe()}")
    print(f"{len(report.listed)} chunk(s) queued")
    for e in report.listed:
        start = (f"+{e.start_ms / 1000:.1f} s (TIME_UNSET)" if e.time_unset
                 else p.iso_ms(e.start_ms))
        print(f"  {e.chunk_id:>8}  {e.session_hex} #{e.sequence:<4} {start:<26} "
              f"{e.duration_ms / 1000:6.1f} s {e.size:>8} B  crc {e.crc32:08x}"
              f"{'  LAST' if e.last else ''}")


def exit_code(report: d.Report) -> int:
    if report.outcome in (d.AUTH_FAILED, d.INCOMPATIBLE):
        return 3
    if report.outcome != d.DONE:
        return 2
    return 1 if report.skipped else 0


def read_token(token_file: Path | None) -> str | None:
    if token_file is not None:
        path = token_file.expanduser()
        if os.name == "posix" and path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
            log.warning("%s is readable by other users; chmod 600 it", path)
        return path.read_text().strip()
    return os.environ.get("CONVO_INGEST_TOKEN", "").strip() or None


def inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


async def main_async(args: argparse.Namespace) -> int:
    mode = d.LIST_ONLY if args.list else d.DRY_RUN if args.dry_run else d.UPLOAD
    uploader = None
    if mode == d.UPLOAD:
        server = args.server or os.environ.get("BARVIS_SERVER")
        token = read_token(args.token_file)
        if not server or not token:
            log.error("need --server (or BARVIS_SERVER) and the token in CONVO_INGEST_TOKEN "
                      "or --token-file")
            return 3
        try:
            uploader = Uploader(server, token, timeout=args.http_timeout)
        except ValueError as e:
            log.error("%s", e)
            return 3
    save_dir = None
    if args.save:
        save_dir = args.save.expanduser()
        if inside_repo(save_dir):
            log.error("--save must be outside the repository (recordings are never committed)")
            return 3
        save_dir.mkdir(parents=True, exist_ok=True)

    state = args.state_dir.expanduser() if args.state_dir else default_state_dir()
    anchors = Anchors(state / "anchors.json", persist=(mode == d.UPLOAD))
    relay = d.Relay(uploader, anchors, mode=mode, save_dir=save_dir)
    once = args.once or args.list or args.dry_run
    backoff = d.Backoff()

    while True:
        log.info("scanning for the watch%s...", f" {args.address}" if args.address else "")
        try:
            device = await find_watch(args.address, args.scan_timeout)
        except Exception as e:  # BleakError, OSError (no adapter, no BlueZ/D-Bus), ...
            log.error("Bluetooth scan failed: %s: %s (adapter present and powered on?)",
                      type(e).__name__, e)
            if once:
                return 2
            await asyncio.sleep(SCAN_ERROR_RETRY_SECONDS)
            continue
        if device is None:
            if once:
                log.error("watch not found. It advertises only when it has something to send "
                          "or its sync window is open (hold BOOT).")
                return NOT_FOUND
            continue
        report = await connect_and_run(relay, device, pair=args.pair)
        attempt = 1
        while once and connect_again(report, attempt):
            attempt += 1
            log.warning("%s; connecting again (%d of %d)", report.message or "link lost",
                        attempt, CONNECT_ATTEMPTS)
            await asyncio.sleep(CONNECT_RETRY_SECONDS)
            report = await connect_and_run(relay, device, pair=args.pair)
        if args.list:
            print_list(report)
        if once:
            if report.outcome != d.DONE and report.message:
                log.error("%s", report.message)
            return exit_code(report)
        if report.outcome == d.DONE:
            backoff.reset()
            delay = LINK_RETRY_SECONDS
        elif report.outcome in (d.AUTH_FAILED, d.INCOMPATIBLE):
            log.error("stopping: %s", report.message)
            return 3
        elif report.outcome == d.LINK_LOST:
            delay = LINK_RETRY_SECONDS
        else:
            delay = backoff.next()
            log.info("retrying in %d s", delay)
        await asyncio.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["pda"]:  # the T5 PDA sync (docs/t5-pda-plan.md), see pda_relay.py
        from pda_relay import main as pda_main

        return pda_main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="See the module docstring for setup and pairing.")
    ap.add_argument("--server", help="server base URL (default: $BARVIS_SERVER)")
    ap.add_argument("--token-file", type=Path,
                    help="file holding the ingest token (default: $CONVO_INGEST_TOKEN)")
    ap.add_argument("--address", help="watch address (macOS: its CoreBluetooth UUID); "
                                      "default: scan for the service")
    ap.add_argument("--pair", action="store_true",
                    help="ask the OS to pair after connecting (Linux with bluetoothctl's agent "
                         "running; type the code shown on the watch)")
    ap.add_argument("--once", action="store_true", help="drain once and exit")
    ap.add_argument("--list", action="store_true",
                    help="show info, status and the queue; change nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and verify chunks, don't upload or ack them")
    ap.add_argument("--save", type=Path, help="with --dry-run: save verified chunks here "
                                              "(outside the repository)")
    ap.add_argument("--scan-timeout", type=float, default=30.0, help="seconds per scan")
    ap.add_argument("--http-timeout", type=float, default=25.0,
                    help="seconds per server request (the watch drops an idle link at 30 s)")
    ap.add_argument("--state-dir", type=Path,
                    help=f"where TIME_UNSET anchors are kept (default: {default_state_dir()})")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)
    if args.save and not args.dry_run:
        ap.error("--save needs --dry-run")
    if args.list and args.dry_run:
        ap.error("--list and --dry-run don't go together")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        import bleak  # noqa: F401
    except ImportError:
        log.error("bleak is missing: pip install -r firmware/recorder/tools/requirements.txt")
        return 3
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
