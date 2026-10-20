#!/usr/bin/env python3
"""Hardware test of the T5's BLE glance service (docs/t5-pda-plan.md §3), from a PC.

A test script, not the relay: the real PC relay is `firmware/recorder/tools/ble_relay.py
pda`. This one checks the firmware's side of the protocol against the plan and needs the
T5 on USB as well, because it reads the pairing code from the console (bleak can't type a
passkey). Windows only for the pairing step (WinRT custom pairing); on Linux pair with
bluetoothctl first and pass --no-pair.

    python pda_ble_test.py --port COM36 pair        # open the window, pair (PIN from the console)
    python pda_ble_test.py --port COM36 sync        # TIME, OB_LIST + OB_ACK, PUT the demo feed, BYE
    python pda_ble_test.py --port COM36 badcrc      # a PUT with a wrong CRC must be refused
    python pda_ble_test.py --port COM36 drop        # disconnect in the middle of a PUT
    python pda_ble_test.py --port COM36 lostack     # list an action, drop the link before OB_ACK

Fictional data only (test/fixtures/).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import serial
from bleak import BleakClient, BleakScanner

BASE = "-51f4-43be-86e4-1653768283cd"
SVC = "d0230000" + BASE
INFO = "d0230001" + BASE
STATUS = "d0230002" + BASE
CTRL = "d0230003" + BASE
DATA = "d0230004" + BASE
FIXTURES = Path(__file__).resolve().parent.parent / "test" / "fixtures"
ERRORS = {
    1: "bad_length",
    2: "unknown_cmd",
    3: "not_found",
    4: "busy",
    5: "mtu_too_small",
    6: "io_error",
    7: "bad_time",
    8: "bad_crc",
    9: "too_large",
    10: "bad_state",
}


class Console:
    """The T5's USB console in a thread: keeps the lines, finds the pairing code."""

    def __init__(self, port: str) -> None:
        self.s = serial.Serial()
        self.s.port, self.s.baudrate, self.s.timeout = port, 115200, 0.1
        self.s.dtr = self.s.rts = False
        self.s.open()
        self.lines: list[str] = []
        self.code: str | None = None
        self.got_code = threading.Event()
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        buf = b""
        while not self._stop:
            buf += self.s.read(512)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip()
                self.lines.append(text)
                m = re.search(r"pairing code (\d{6})", text)
                if m:
                    self.code = m.group(1)
                    self.got_code.set()

    def cmd(self, line: str) -> None:
        self.s.write(line.encode() + b"\r\n")

    def grep(self, pattern: str) -> list[str]:
        return [x for x in self.lines if re.search(pattern, x)]

    def close(self) -> None:
        self._stop = True
        time.sleep(0.2)
        self.s.close()


class NullConsole:
    lines: list[str] = []

    def cmd(self, line: str) -> None:
        pass

    def grep(self, pattern: str) -> list[str]:
        return []

    def close(self) -> None:
        pass


async def find_t5(timeout: float = 20.0):
    seen = {}

    def cb(dev, adv):
        if SVC in (adv.service_uuids or []):
            seen[dev.address] = (dev, adv)

    async with BleakScanner(detection_callback=cb):
        t_end = time.monotonic() + timeout
        while not seen and time.monotonic() < t_end:
            await asyncio.sleep(0.5)
        await asyncio.sleep(2.0)  # let the scan response arrive too
    if not seen:
        raise SystemExit("no T5 advertising the glance service")
    dev, adv = next(iter(seen.values()))
    sd = (adv.service_data or {}).get(SVC)
    print(
        f"found {dev.address} name={adv.local_name!r} rssi={adv.rssi} "
        f"service_data={sd.hex() if sd else None}"
    )
    return dev, adv


async def winrt_pair(address: str, con: Console) -> None:
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import (
        DevicePairingKinds,
        DevicePairingProtectionLevel,
    )

    dev = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":", ""), 16))
    info = dev.device_information
    if info.pairing.is_paired:
        print("already paired with Windows; unpairing first")
        await info.pairing.unpair_async()
    custom = info.pairing.custom

    def on_request(sender, args):
        kind = args.pairing_kind
        if kind == DevicePairingKinds.PROVIDE_PIN:
            con.got_code.wait(30)
            print(f"pairing: typing the code from the T5 console ({con.code})")
            args.accept_with_pin(con.code)
        else:
            print(f"pairing: kind {kind}, accepting")
            args.accept()

    token = custom.add_pairing_requested(on_request)
    try:
        result = await custom.pair_with_protection_level_async(
            DevicePairingKinds.PROVIDE_PIN | DevicePairingKinds.CONFIRM_ONLY,
            DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION,
        )
        print(f"pairing result: {result.status} (protection {result.protection_level_used})")
    finally:
        custom.remove_pairing_requested(token)


class Link:
    """One connection: responses from `data` in a queue, status notifications kept."""

    def __init__(self, client: BleakClient) -> None:
        self.c = client
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.status: list[bytes] = []

    async def start(self) -> None:
        await self.c.start_notify(DATA, lambda _, d: self.q.put_nowait(bytes(d)))
        await self.c.start_notify(STATUS, lambda _, d: self.status.append(bytes(d)))

    async def cmd(self, b: bytes, expect: int | None = None, timeout: float = 10.0) -> bytes | None:
        await self.c.write_gatt_char(CTRL, b, response=True)
        if expect is None:
            return None
        r = await asyncio.wait_for(self.q.get(), timeout)
        if r[0] == 0xEE:
            code = r[2]
            arg = struct.unpack_from("<I", r, 4)[0]
            print(f"  ERROR cmd=0x{r[1]:02x} {ERRORS.get(code, code)} arg={arg}")
        return r

    async def read_status(self) -> dict:
        b = bytes(await self.c.read_gatt_char(STATUS))
        proto, flags, batt, _, ob, _, ver, boot = struct.unpack("<BBbBHHII", b)
        return {
            "proto": proto,
            "flags": flags,
            "battery": batt,
            "outbox": ob,
            "feed_version": ver,
            "boot_id": boot,
        }


def time_cmd(tz_min: int = 120) -> bytes:
    return struct.pack("<B3xqh", 0x04, int(time.time() * 1000), tz_min)


async def put_blob(
    link: Link,
    kind: int,
    blob_id: int,
    data: bytes,
    mtu: int,
    crc: int | None = None,
    stop_after: int | None = None,
) -> bytes | None:
    crc = zlib.crc32(data) & 0xFFFFFFFF if crc is None else crc
    r = await link.cmd(struct.pack("<BB2xIII", 0x10, kind, blob_id, len(data), crc), expect=True)
    if r[0] != 0x90:
        return r
    step = min(mtu - 3, 512) - 5  # 512-byte attribute cap
    t0 = time.monotonic()
    for off in range(0, len(data), step):
        if stop_after is not None and off >= stop_after:
            return None
        await link.cmd(struct.pack("<BI", 0x11, off) + data[off : off + step])
        if not link.q.empty():  # an ERROR in the middle
            return await link.q.get()
    r = await link.cmd(b"\x12", expect=True)
    dt = time.monotonic() - t0
    print(
        f"  PUT {len(data)} bytes in {dt:.2f} s ({len(data) / dt / 1024:.1f} KB/s, {step} B/write)"
    )
    return r


async def connect(address: str) -> tuple[BleakClient, Link, int]:
    client = BleakClient(address, timeout=20.0)
    await client.connect()
    # WinRT reports the negotiated MTU a moment after the connection is up.
    for _ in range(30):
        if client.mtu_size > 23:
            break
        await asyncio.sleep(0.1)
    mtu = client.mtu_size
    print(f"connected, MTU {mtu}")
    link = Link(client)
    await link.start()
    info = (await client.read_gatt_char(INFO)).decode()
    print(f"info: {info}")
    assert "proto=1" in info
    return client, link, mtu


async def ob_drain(link: Link, ack: bool, result: int = 0) -> list[int]:
    ids, after = [], 0
    while True:
        await link.c.write_gatt_char(CTRL, struct.pack("<BIH", 0x20, after, 16), response=True)
        while True:
            r = await asyncio.wait_for(link.q.get(), 10)
            if r[0] == 0xA0:
                n, aid, created = struct.unpack_from("<HIQ", r, 2)
                print(f"  OB_ENTRY {aid:08x} created {created} {r[16 : 16 + n].decode()}")
                ids.append(aid)
                after = aid
            elif r[0] == 0xA1:
                count, remaining = struct.unpack_from("<HI", r, 2)
                print(f"  OB_END count={count} remaining={remaining}")
                break
            else:
                print(f"  unexpected {r.hex()}")
                return ids
        if remaining == 0:
            break
    if ack:
        for aid in ids:
            r = await link.cmd(struct.pack("<BIB", 0x21, aid, result), expect=True)
            print(f"  OB_ACK {aid:08x} -> result {r[1]}")
    return ids


async def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--port", required=True, help="the T5's USB console (check the MAC first)")
    p.add_argument("what", choices=["scan", "pair", "sync", "badcrc", "drop", "lostack", "big"])
    p.add_argument("--no-ack", action="store_true")
    p.add_argument(
        "--no-console",
        action="store_true",
        help="don't open the USB console (to capture it elsewhere); no pairing",
    )
    a = p.parse_args()
    con = NullConsole() if a.no_console else Console(a.port)
    try:
        if a.what == "pair":
            con.cmd("ble pair")
            await asyncio.sleep(1)
        dev, adv = await find_t5()
        if a.what == "scan":
            return 0
        if a.what == "pair":
            await winrt_pair(dev.address, con)
            await asyncio.sleep(1)
        client, link, mtu = await connect(dev.address)
        try:
            print("status:", await link.read_status())
            r = await link.cmd(time_cmd(), expect=True)
            print(f"TIME -> {r.hex()}")
            if a.what in ("sync", "pair"):
                await ob_drain(link, ack=not a.no_ack)
                feed = (FIXTURES / "feed_demo.json").read_bytes()
                r = await put_blob(link, 1, 1792479600, feed, mtu)
                print(f"PUT feed -> {r.hex()}")
                print("status:", await link.read_status())
            elif a.what == "badcrc":
                feed = (FIXTURES / "feed_demo.json").read_bytes()
                r = await put_blob(link, 1, 99, feed, mtu, crc=0xDEADBEEF)
                print(f"PUT bad crc -> {r.hex()}")
                assert r[0] == 0xEE and r[2] == 8
            elif a.what == "big":
                big = (
                    b'{"v":1,"ref":"transcript:big@0-600","title":"200 KB test","pad":"'
                    + b"x" * 200_000
                    + b'"}'
                )
                r = await put_blob(link, 2, 7, big, mtu)
                print(f"PUT 200 KB -> {r.hex()}")
            elif a.what == "drop":
                feed = (FIXTURES / "feed_demo.json").read_bytes()
                await put_blob(link, 1, 98, feed, mtu, stop_after=1000)
                print("disconnecting in the middle of the PUT")
                await client.disconnect()
                return 0
            elif a.what == "lostack":
                ids = await ob_drain(link, ack=False)
                print(f"listed {len(ids)}, disconnecting without OB_ACK")
                await client.disconnect()
                return 0
            r = await link.cmd(b"\x05", expect=True)
            print(f"BYE -> {r.hex()}")
            print(f"status notifications: {len(link.status)}")
        finally:
            if client.is_connected:
                await asyncio.sleep(1.5)
                await client.disconnect()
        return 0
    finally:
        await asyncio.sleep(0.5)
        for line in con.grep(r"\b(ble|pda):"):
            print("  T5:", line)
        con.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
