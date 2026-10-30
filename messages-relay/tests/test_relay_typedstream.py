from __future__ import annotations

from athena_messages_relay import typedstream

from conftest import ts_blob


def test_short_text():
    assert typedstream.decode(ts_blob("Dinner at 7?")) == "Dinner at 7?"


def test_text_longer_than_127_bytes_uses_a_two_byte_length():
    text = "Leaving now, " * 30
    assert typedstream.decode(ts_blob(text)) == text


def test_very_long_text_uses_a_four_byte_length():
    text = "x" * 70000
    assert typedstream.decode(ts_blob(text)) == text


def test_unicode():
    assert typedstream.decode(ts_blob("Café ☕ 🎉 ok")) == "Café ☕ 🎉 ok"


def test_unreadable_blobs_give_none():
    assert typedstream.decode(None) is None
    assert typedstream.decode(b"") is None
    assert typedstream.decode(b"streamtyped no string here") is None
    blob = ts_blob("hello")
    assert typedstream.decode(blob[:blob.index(b"hello") + 2]) is None  # cut short
    assert typedstream.decode(blob.replace(b"\x01+", b"\x01-")) is None  # no marker
