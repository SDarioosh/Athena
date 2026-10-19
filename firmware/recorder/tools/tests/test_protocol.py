"""The test vectors of docs/twatch-ble-protocol.md §8, byte for byte."""

from __future__ import annotations

import pytest
from barvis_relay import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


SESSION = h("3f9a0c11d2e4b507")


def test_command_vectors() -> None:
    assert p.encode_list(0, 32) == h("01 00000000 2000")
    assert p.encode_read(7, 0, 0) == h("02 07000000 00000000 00000000")
    assert p.encode_ack(7) == h("03 07000000")
    assert p.encode_time(1792411200000) == h("04 000000 006a0854a1010000")
    assert p.encode_bye() == h("05")
    assert all(len(c) <= 20 for c in (p.encode_list(0), p.encode_read(1), p.encode_time(0)))


RESPONSES = [
    (p.Entry(7, SESSION, 2, 1792411260000, 12340, 24680, 0xCBF43926, p.FLAG_LAST),
     "81 01 0000 07000000 3f9a0c11d2e4b507 02000000 60540954a1010000 34300000 68600000 "
     "2639f4cb"),
    (p.DataFrame(7, 505, h("aabb")), "82 07000000 f9010000 aabb"),
    (p.ReadEnd(0, 7, 24680, 0xCBF43926), "83 00 0000 07000000 68600000 2639f4cb"),
    (p.Acked(0, 7), "84 00 0000 07000000"),
    (p.ListEnd(3, 5), "85 00 0300 05000000"),
    (p.TimeOk(2), "86 00 0200"),
    (p.Error(p.OP_READ, p.ERR_NOT_FOUND, 9), "ee 02 03 00 09000000"),
    (p.ByeOk(), "87"),
]


@pytest.mark.parametrize(("msg", "hexstr"), RESPONSES)
def test_response_vectors(msg, hexstr: str) -> None:
    assert msg.encode() == h(hexstr)
    assert p.decode_response(h(hexstr)) == msg


def test_entry_fields() -> None:
    e = p.decode_response(h(RESPONSES[0][1]))
    assert isinstance(e, p.Entry)
    assert e.last and not e.time_unset
    assert e.session_hex == "3f9a0c11d2e4b507"
    assert len(e.encode()) == p.Entry.LEN


def test_status_vector() -> None:
    raw = h("01 25 51 00 6e0f 0200 c0d40100 60ea0000 4006d66a 04030201")
    s = p.Status.decode(raw)
    assert s == p.Status(1, p.ST_RECORDING | p.ST_TIME_VALID | p.ST_SD_OK, 81, 3950, 2,
                         120000, 60000, 1792411200, 0x01020304)
    assert s.encode() == raw
    assert "recording" in s.describe() and "81%" in s.describe()


def test_info_parse() -> None:
    i = p.Info.parse(b"proto=1;device=twatch-ultra;fw=0.2.0;board=twatch_ultra")
    assert (i.proto, i.device, i.fw, i.board) == (1, "twatch-ultra", "0.2.0", "twatch_ultra")
    i = p.Info.parse("board=x;new=1;device=d;proto=2")  # any order, unknown keys kept aside
    assert (i.proto, i.device, i.extra) == (2, "d", {"new": "1"})
    with pytest.raises(p.ProtocolError):
        p.Info.parse("device=d")
    with pytest.raises(p.ProtocolError):
        p.Info.parse("proto=1")


def test_decode_errors_and_growth() -> None:
    with pytest.raises(p.ProtocolError):
        p.decode_response(b"")
    with pytest.raises(p.ProtocolError):
        p.decode_response(h("99"))
    with pytest.raises(p.ProtocolError):
        p.decode_response(h("84 00 0000"))
    # §9: a field added at the end is compatible
    assert p.decode_response(h("84 01 0000 07000000 ffff")) == p.Acked(1, 7)


def test_iso_ms() -> None:
    assert p.iso_ms(1792411200000) == "2026-10-19T12:00:00.000Z"
    assert p.iso_ms(1792411260789) == "2026-10-19T12:01:00.789Z"
    assert p.iso_ms(0) == "1970-01-01T00:00:00.000Z"
