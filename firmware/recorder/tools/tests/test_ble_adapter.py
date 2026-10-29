"""ble_relay.BleakTransport over a bleak-shaped fake client (no Bluetooth needed)."""

from __future__ import annotations

import asyncio

import ble_relay
from athena_relay import drain as d
from athena_relay import protocol as p
from athena_relay.anchors import Anchors
from athena_relay.uploader import Uploader
from conftest import TOKEN, FakeWatch, make_session


class FakeBleakClient:
    """The subset of bleak.BleakClient that BleakTransport uses, backed by a FakeWatch."""

    def __init__(self, watch: FakeWatch, on_disconnect) -> None:
        self.watch = watch
        self.on_disconnect = on_disconnect
        self.notify: dict[str, object] = {}
        self.is_connected = True
        self.writes: list[tuple[str, bytes, bool]] = []

    async def start_notify(self, uuid: str, callback) -> None:
        self.notify[uuid] = callback

    async def read_gatt_char(self, uuid: str) -> bytearray:
        if uuid == p.INFO_UUID:
            return bytearray(await self.watch.read_info())
        assert uuid == p.STATUS_UUID
        return bytearray(await self.watch.read_status())

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self.writes.append((uuid, bytes(data), response))
        await self.watch.write(data)
        # deliver the watch's answers as notifications, like the BLE stack would
        while not self.watch.queue.empty():
            item = self.watch.queue.get_nowait()
            if item is None:
                self.is_connected = False
                self.on_disconnect()
                return
            await asyncio.sleep(0)
            self.notify[p.DATA_UUID](None, bytearray(item))


def drain_via_adapter(watch: FakeWatch, relay: d.Relay) -> tuple[d.Report, FakeBleakClient]:
    async def go():
        watch.reconnect()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        client = FakeBleakClient(watch, lambda: queue.put_nowait(None))
        t = ble_relay.BleakTransport(client, queue)
        await t.open()
        return await relay.run(t), client

    return asyncio.run(go())


def test_drain_through_bleak_transport(server) -> None:
    w = FakeWatch()
    make_session(w, 3, size=4000)
    relay = d.Relay(Uploader(server.url, TOKEN), Anchors(None), timeout=2.0)
    rep, client = drain_via_adapter(w, relay)
    assert rep.outcome == d.DONE and rep.acked == [1, 2, 3]
    assert set(client.notify) == {p.DATA_UUID, p.STATUS_UUID}
    assert all(u == p.CTRL_UUID and resp for u, _, resp in client.writes)


def test_disconnect_through_bleak_transport_then_resume(server) -> None:
    w = FakeWatch(drop_after_frames={1: 2})
    make_session(w, 2, size=4000)
    relay = d.Relay(Uploader(server.url, TOKEN), Anchors(None), timeout=2.0)
    rep, _ = drain_via_adapter(w, relay)
    assert rep.outcome == d.LINK_LOST
    rep, _ = drain_via_adapter(w, relay)
    assert rep.outcome == d.DONE and rep.acked == [1, 2] and w.reads[1][1] > 0


def test_exit_codes_and_repo_guard(tmp_path) -> None:
    assert ble_relay.exit_code(d.Report()) == 0
    assert ble_relay.exit_code(d.Report(skipped={1: "x"})) == 1
    assert ble_relay.exit_code(d.Report(outcome=d.SERVER_RETRY)) == 2
    assert ble_relay.exit_code(d.Report(outcome=d.AUTH_FAILED)) == 3
    assert ble_relay.inside_repo(ble_relay.REPO_ROOT / "firmware" / "x")
    assert not ble_relay.inside_repo(tmp_path)


def test_connect_again_only_before_the_link_carried_anything() -> None:
    lost = d.Report(outcome=d.LINK_LOST, message="connect failed")
    assert ble_relay.connect_again(lost, 1)
    assert not ble_relay.connect_again(lost, ble_relay.CONNECT_ATTEMPTS)
    info = p.Info(proto=1, device="twatch-ultra", fw="0.2.0", board="twatch_ultra")
    assert not ble_relay.connect_again(d.Report(outcome=d.LINK_LOST, info=info), 1)
    assert not ble_relay.connect_again(d.Report(outcome=d.WATCH_RETRY), 1)
    assert not ble_relay.connect_again(d.Report(), 1)


def test_token_file(tmp_path, monkeypatch) -> None:
    f = tmp_path / "tok"
    f.write_text(TOKEN + "\n")
    f.chmod(0o600)
    assert ble_relay.read_token(f) == TOKEN
    monkeypatch.setenv("CONVO_INGEST_TOKEN", " env-token ")
    assert ble_relay.read_token(None) == "env-token"
