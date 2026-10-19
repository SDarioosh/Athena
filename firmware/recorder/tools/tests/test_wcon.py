"""Tests for the console helper's output handling (no serial port needed)."""

import wcon


def test_has_prompt_at_end_only():
    assert wcon.has_prompt(b"I (12) x: y\r\nrecorder> ")
    assert not wcon.has_prompt(b"recorder> status\r\nbattery 81 %\r\n")


def test_strip_echo_drops_command_and_prompt():
    raw = "status\r\nrecording: no\r\nbattery: 81 %\r\nrecorder> "
    assert wcon.strip_echo(raw, "status") == "recording: no\nbattery: 81 %"


def test_strip_echo_keeps_log_lines():
    raw = "recorder> note start\nI (5) session: note started\nrecorder> "
    assert wcon.strip_echo(raw, "note start") == "I (5) session: note started"


class FakePort:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""


def test_read_until_prompt_stops_at_prompt():
    port = FakePort([b"a\n", b"recorder> ", b"never"])
    assert wcon.read_until_prompt(port, timeout=1.0) == b"a\nrecorder> "


def test_read_until_prompt_times_out_on_silence():
    port = FakePort([b"partial"])
    assert wcon.read_until_prompt(port, timeout=0.3) == b"partial"
