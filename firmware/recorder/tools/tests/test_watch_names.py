"""The relay finds the watch under its current Bluetooth name `athena-tw` and under the
older firmware's `barvis-tw`; the service UUID stays the main key."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import ble_relay
import pytest
from athena_relay import protocol as p


@pytest.mark.parametrize("name", ["athena-tw", "barvis-tw"])
def test_both_names_are_the_watch(name: str) -> None:
    assert p.is_watch_name(name)
    assert p.is_watch([], name)


@pytest.mark.parametrize("name", [None, "", "odysseus-pda", "Athena-TW", "barvis-tw2"])
def test_other_names_are_not(name: str | None) -> None:
    assert not p.is_watch_name(name)
    assert not p.is_watch([], name)


@pytest.mark.parametrize("name", [None, "athena-tw", "barvis-tw", "something else"])
def test_service_uuid_is_enough(name: str | None) -> None:
    assert p.is_watch([p.SERVICE_UUID.upper()], name)


def test_find_watch_accepts_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeScanner:
        @staticmethod
        async def find_device_by_filter(match, timeout, service_uuids):
            seen["service_uuids"] = service_uuids
            seen["match"] = match

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakScanner=FakeScanner))
    asyncio.run(ble_relay.find_watch(None, 1.0))
    assert seen["service_uuids"] == [p.SERVICE_UUID]
    match = seen["match"]

    def adv(uuids: list[str], name: str | None) -> SimpleNamespace:
        return SimpleNamespace(service_uuids=uuids, local_name=name)

    assert match(None, adv([p.SERVICE_UUID], "athena-tw"))
    assert match(None, adv([p.SERVICE_UUID], "barvis-tw"))
    assert match(None, adv([], "athena-tw"))
    assert match(None, adv([], "barvis-tw"))
    assert not match(None, adv([], "odysseus-pda"))
    assert not match(None, adv([], None))
