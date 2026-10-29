#!/usr/bin/env python3
"""PC stand-in for the phone relay: sync the T5 E-Paper Pro PDA over BLE.

Speaks the "glance" protocol of docs/t5-pda-plan.md §3 with bleak: scan for the T5's
service, send TIME, check the server's /health, then the outbox first (each action is
checked, sent to the server and acked with its result), then the feed (§4.1, PUT only when
it changed), then the detail blobs of `fetch` actions, and BYE. An action is acked only
after the server answered; a temporary failure leaves it queued on the T5. The T5 holds no
token; the data goes server -> this PC -> BLE and never touches the cloud.

Run it through the watch relay's CLI (`ble_relay.py pda ...`) or directly. The token and
server come from the same places as for the watch drain: CONVO_INGEST_TOKEN or
--token-file, --server or BARVIS_SERVER.

Pairing works like the watch's (see ble_relay.py --help): open the T5's pairing window on
its Status screen, then pair through the OS and type the code the T5 shows ("odysseus-pda").

Look at the T5 without changing anything:

    python firmware/recorder/tools/ble_relay.py pda --list

Sync once and exit (exit code 0 = everything delivered):

    export BARVIS_SERVER=http://<server>:8000 CONVO_INGEST_TOKEN=... BARVIS_TZ=Europe/Berlin
    export ODYSSEUS_URL=http://127.0.0.1:7010 ODYSSEUS_TOKEN=ody_...   # or --odysseus-token-file
    python firmware/recorder/tools/ble_relay.py pda --once --calendar-href <calendar id> \\
        --templates "Call,Meeting,Reminder"

The same URL and token also give the T5's Tasks tab the owner's Odysseus checklists
(plan §4.4; the token needs todos:read and todos:write too) and the Today screen the hub's
Today insight and its rule weights (plan §4.1, §5; the token needs insight:read too; the last
good weights table is kept in the state dir's pda.json when a later fetch fails). Without
--odysseus-url and the token, calendar and checklist actions stay queued on the T5, the
feed's calendar is empty and it has no todos, no insight and no weights.

Exit codes for --once/--list: 0 ok, 1 some actions still waiting, 2 retry later (server,
link or T5 error), 3 token rejected / incompatible / bad settings, 4 T5 not found.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from athena_relay import pda_protocol as q
from athena_relay import pda_sync as s
from athena_relay.anchors import default_state_dir
from athena_relay.pda_actions import NoCalendar
from athena_relay.pda_calendar import OdysseusCalendar
from athena_relay.pda_feed import HttpApi, resolve_tz
from athena_relay.pda_insight import OdysseusInsight
from athena_relay.pda_todos import OdysseusTodos
from athena_relay.protocol import iso_ms

log = logging.getLogger("athena_relay.pda_ble")
NOT_FOUND = 4
LINK_RETRY_SECONDS = 5
SCAN_ERROR_RETRY_SECONDS = 30
RECONNECT_SECONDS = 15 * 60  # §3.4 step 6
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_SECONDS = 2
REQUEST_MTU = 517


class PdaBleakTransport:
    """pda_sync.Transport over a connected bleak client."""

    def __init__(self, client, data: asyncio.Queue[bytes | None],
                 status: asyncio.Queue[bytes | None]) -> None:
        self.client = client
        self.data = data
        self.status = status
        self.mtu = 0

    async def open(self) -> None:
        def on_data(_char, value: bytearray) -> None:
            self.data.put_nowait(bytes(value))

        def on_status(_char, value: bytearray) -> None:
            self.status.put_nowait(bytes(value))

        # Both need an encrypted link: a new relay pairs here (§3, watch protocol §3.1).
        await self._gatt(self.client.start_notify(q.DATA_UUID, on_data))
        await self._gatt(self.client.start_notify(q.STATUS_UUID, on_status))
        backend = str(getattr(self.client, "backend_id", "")).lower()
        if "bluez" in backend:  # BlueZ reports 23 until the MTU is acquired
            with contextlib.suppress(Exception):
                await self.client._backend._acquire_mtu()
        with contextlib.suppress(Exception):
            self.mtu = int(self.client.mtu_size)
        log.info("ATT MTU %s", self.mtu or "unknown")

    async def _gatt(self, aw):
        try:
            from bleak.exc import BleakError
        except ImportError:  # only in tests without bleak
            BleakError = OSError  # noqa: N806
        try:
            return await aw
        except (BleakError, OSError, EOFError) as e:
            raise s.LinkLost(f"{type(e).__name__}: {e}") from None

    async def read_info(self) -> bytes:
        return bytes(await self._gatt(self.client.read_gatt_char(q.INFO_UUID)))

    async def read_status(self) -> bytes:
        return bytes(await self._gatt(self.client.read_gatt_char(q.STATUS_UUID)))

    async def write(self, command: bytes) -> None:
        if not self.client.is_connected:
            raise s.LinkLost("disconnected")
        await self._gatt(self.client.write_gatt_char(q.CTRL_UUID, command, response=True))

    async def receive(self, timeout: float) -> bytes:
        return await self._get(self.data, timeout)

    async def wait_status(self, timeout: float) -> bytes:
        return await self._get(self.status, timeout)

    async def _get(self, queue: asyncio.Queue[bytes | None], timeout: float) -> bytes:
        item = await asyncio.wait_for(queue.get(), timeout)
        if item is None:
            queue.put_nowait(None)
            raise s.LinkLost("the T5 disconnected")
        return item

    def discard_pending(self) -> int:
        n = 0
        while not self.data.empty():
            if self.data.get_nowait() is None:
                self.data.put_nowait(None)  # keep the disconnect marker
                break
            n += 1
        return n


def _service_data(adv) -> bytes | None:
    for uuid, value in (getattr(adv, "service_data", None) or {}).items():
        if uuid.lower() == q.SERVICE_UUID:
            return bytes(value)
    return None


async def find_pda(address: str | None, timeout: float, *, outbox_only: bool = False):
    """The T5 (by address or service UUID). `outbox_only`: only once it advertises
    "outbox waiting" in its scan response."""
    from bleak import BleakScanner

    def match(dev, adv) -> bool:
        if address and dev.address.lower() != address.lower():
            return False
        if not address and q.SERVICE_UUID not in [u.lower() for u in adv.service_uuids]:
            return False
        return not outbox_only or q.outbox_waiting(_service_data(adv))

    return await BleakScanner.find_device_by_filter(
        match, timeout=timeout, service_uuids=None if address else [q.SERVICE_UUID])


async def connect_and_run(sync: s.PdaSync, device, *, pair: bool) -> s.Report:
    from bleak import BleakClient
    from bleak.exc import BleakError

    data: asyncio.Queue[bytes | None] = asyncio.Queue()
    status: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_disconnect(_client) -> None:
        loop.call_soon_threadsafe(data.put_nowait, None)
        loop.call_soon_threadsafe(status.put_nowait, None)

    client = BleakClient(device, disconnected_callback=on_disconnect, timeout=20)
    try:
        await client.connect()
    except (BleakError, OSError, asyncio.TimeoutError, TimeoutError) as e:  # noqa: UP041
        return s.Report(outcome=s.LINK_LOST, message=f"connect failed: {type(e).__name__}: {e}")
    try:
        log.info("connected to %s", client.address)
        if pair:
            try:
                await client.pair()
                log.info("paired (bonded) with %s", client.address)
            except NotImplementedError:
                log.info("this OS pairs on first encrypted access; accept the system prompt")
            except BleakError as e:
                log.error("pairing failed: %s. Open the T5's pairing window and pair through "
                          "the OS so you can type the code from its screen", e)
        t = PdaBleakTransport(client, data, status)
        try:
            await t.open()
        except s.LinkLost as e:
            return s.Report(outcome=s.LINK_LOST,
                            message=f"subscribe failed ({e}); not paired? Open the T5's "
                                    "pairing window and pair through the OS")
        sync.device_key = client.address
        return await sync.run(t)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def print_list(report: s.Report) -> None:
    if report.info:
        i = report.info
        print(f"T5      device={i.device} fw={i.fw or '?'} board={i.board or '?'} "
              f"proto={i.proto}")
    if report.status:
        print(f"status  {report.status.describe()}")
    print(f"{len(report.listed)} action(s) queued")
    for e in report.listed:
        created = iso_ms(e.created_ms) if e.created_ms else "time unknown"
        print(f"  {e.action_id:>8}  {created}  {e.json.decode('utf-8', 'replace')}")


def exit_code(report: s.Report) -> int:
    if report.outcome in (s.AUTH_FAILED, s.INCOMPATIBLE):
        return 3
    if report.outcome != s.DONE:
        return 2
    return 1 if report.waiting else 0


def make_odysseus(args: argparse.Namespace):
    """(calendar, todos, insight): OdysseusCalendar, OdysseusTodos and OdysseusInsight if a
    URL and token are set, (NoCalendar, None, None) if neither, None (an error) if only one
    is."""
    from ble_relay import read_token

    url = args.odysseus_url or os.environ.get("ODYSSEUS_URL")
    if args.odysseus_token_file is not None:
        token = read_token(args.odysseus_token_file)
    else:
        token = os.environ.get("ODYSSEUS_TOKEN", "").strip() or None
    if not url and not token:
        return NoCalendar(), None, None
    if not url or not token:
        log.error("Odysseus (calendar, todos, insight) needs both --odysseus-url (or ODYSSEUS_URL) "
                  "and the token in ODYSSEUS_TOKEN or --odysseus-token-file")
        return None
    if not args.calendar_href:
        log.warning("no --calendar-href: Odysseus puts new events in the owner's first "
                    "calendar, which may not be the CalDAV one")
    try:
        return (OdysseusCalendar(url, token, calendar_href=args.calendar_href,
                                 timeout=args.http_timeout),
                OdysseusTodos(url, token, timeout=args.http_timeout),
                OdysseusInsight(url, token, timeout=args.http_timeout))
    except ValueError as e:
        log.error("--odysseus-url: %s", e)
        return None


async def main_async(args: argparse.Namespace) -> int:
    from ble_relay import read_token

    server = args.server or os.environ.get("BARVIS_SERVER")
    token = read_token(args.token_file)
    if not args.list and (not server or not token):
        log.error("need --server (or BARVIS_SERVER) and the token in CONVO_INGEST_TOKEN "
                  "or --token-file")
        return 3
    tz_name = args.tz or os.environ.get("BARVIS_TZ") or "UTC"
    try:
        resolve_tz(tz_name)
        api = HttpApi(server or "http://unused.invalid", token or "unused",
                      timeout=args.http_timeout)
    except ValueError as e:
        log.error("%s", e)
        return 3
    odysseus = make_odysseus(args)
    if odysseus is None:
        return 3
    calendar, todos, insight = odysseus
    templates = [x.strip() for x in (args.templates or "").split(",") if x.strip()]
    state_dir = args.state_dir.expanduser() if args.state_dir else default_state_dir()
    state = s.PdaState(None if args.list else state_dir / "pda.json")
    once = args.once or args.list
    sync = s.PdaSync(api, tz_name=tz_name, calendar=calendar, templates=templates, todos=todos,
                     insight=insight, state=state, stay=not once, list_only=args.list)
    backoff = s.Backoff()
    next_due = 0.0
    early = False  # after a good sync: connect before next_due if the outbox is waiting

    while True:
        wait = next_due - time.monotonic()
        if wait > 0 and not early:  # backing off: don't connect before it's due
            await asyncio.sleep(wait)
            wait = 0
        outbox_only = wait > 0
        log.info("scanning for the T5%s%s...", f" {args.address}" if args.address else "",
                 " (outbox waiting only)" if outbox_only else "")
        try:
            device = await find_pda(args.address, min(args.scan_timeout, wait)
                                    if outbox_only else args.scan_timeout,
                                    outbox_only=outbox_only)
        except Exception as e:  # BleakError, OSError (no adapter, no BlueZ/D-Bus), ...
            log.error("Bluetooth scan failed: %s: %s (adapter present and powered on?)",
                      type(e).__name__, e)
            if once:
                return 2
            await asyncio.sleep(SCAN_ERROR_RETRY_SECONDS)
            continue
        if device is None:
            if once:
                log.error("T5 not found. Is it awake, and advertising (its Status screen)?")
                return NOT_FOUND
            continue
        report = await connect_and_run(sync, device, pair=args.pair)
        attempt = 1
        while once and report.outcome == s.LINK_LOST and report.info is None \
                and attempt < CONNECT_ATTEMPTS:
            attempt += 1
            log.warning("%s; connecting again (%d of %d)", report.message or "link lost",
                        attempt, CONNECT_ATTEMPTS)
            await asyncio.sleep(CONNECT_RETRY_SECONDS)
            report = await connect_and_run(sync, device, pair=args.pair)
        if args.list:
            print_list(report)
        if once:
            if report.outcome != s.DONE and report.message:
                log.error("%s", report.message)
            return exit_code(report)
        early = report.outcome == s.DONE
        if report.outcome == s.DONE:
            backoff.reset()
            delay = RECONNECT_SECONDS
        elif report.outcome in (s.AUTH_FAILED, s.INCOMPATIBLE):
            log.error("stopping: %s", report.message)
            return 3
        elif report.outcome == s.LINK_LOST:
            delay = LINK_RETRY_SECONDS
        else:
            delay = backoff.next()
        log.info("next sync in %d s (sooner if the T5 says its outbox is waiting)", delay)
        next_due = time.monotonic() + delay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ble_relay.py pda", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="See the docstring of pda_relay.py for setup.")
    ap.add_argument("--server", help="server base URL (default: $BARVIS_SERVER)")
    ap.add_argument("--token-file", type=Path,
                    help="file holding the server token (default: $CONVO_INGEST_TOKEN)")
    ap.add_argument("--address", help="T5 address (macOS: its CoreBluetooth UUID); "
                                      "default: scan for the service")
    ap.add_argument("--pair", action="store_true", help="ask the OS to pair after connecting")
    ap.add_argument("--once", action="store_true", help="sync once and exit")
    ap.add_argument("--list", action="store_true",
                    help="show info, status and the outbox; change nothing")
    ap.add_argument("--tz", help="IANA time zone for the feed and the T5 clock "
                                 "(default: $BARVIS_TZ or UTC)")
    ap.add_argument("--odysseus-url", help="Odysseus base URL for the calendar "
                                           "(default: $ODYSSEUS_URL; none = no calendar)")
    ap.add_argument("--odysseus-token-file", type=Path,
                    help="file holding the calendar-scoped Odysseus token "
                         "(default: $ODYSSEUS_TOKEN)")
    ap.add_argument("--calendar-href", help="Odysseus calendar id that new events go to")
    ap.add_argument("--templates", help="comma-separated event titles for the T5's \"+\" "
                                        "picker, e.g. \"Call,Meeting,Reminder\"")
    ap.add_argument("--scan-timeout", type=float, default=30.0, help="seconds per scan")
    ap.add_argument("--http-timeout", type=float, default=25.0,
                    help="seconds per server request")
    ap.add_argument("--state-dir", type=Path,
                    help=f"where the last feed hash is kept (default: {default_state_dir()})")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

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
