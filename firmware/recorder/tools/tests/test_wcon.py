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


def test_prompt_followed_by_async_logs_counts():
    # ESP-IDF logs are asynchronous: a log line can land after the prompt came back.
    buf = b"status\r\nbattery: 81 %\r\nrecorder> \r\nI (900) ble: adv start\r\n"
    assert wcon.has_prompt(buf, "status")
    buf = b"recorder> note start\nI (5) session: note started\nrecorder> I (6) x: y\n"
    assert wcon.has_prompt(buf, "note start")


def test_echoed_command_is_not_the_prompt():
    assert not wcon.has_prompt(b"recorder> status\r\nbattery 81 %\r\n", "status")
    assert not wcon.has_prompt(b"recorder> status\r\nrecorder> sta", "status")


def test_read_until_prompt_stops_when_logs_follow_the_prompt():
    port = FakePort([b"status\r\nok\r\nrecorder> ", b"I (9) x: late log\r\n", b"never"])
    assert wcon.read_until_prompt(port, timeout=1.0, cmd="status") == (
        b"status\r\nok\r\nrecorder> ")
    port = FakePort([b"status\r\nok\r\nrecorder> \r\nI (9) x: late log\r\n", b"more"])
    assert wcon.read_until_prompt(port, timeout=1.0, cmd="status").endswith(b"late log\r\n")


class GonePort(FakePort):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.closed = 0

    def close(self):
        self.closed += 1


def test_listen_does_not_reopen_a_quiet_windows_port(monkeypatch, capsys):
    # On Windows "COM35" isn't a path, so os.path.exists() is False: a quiet port must not
    # be taken for one that went away.
    port = GonePort([b"I (1) a: b\n"])
    opens = []

    def fake_open(name, wait=15.0):
        opens.append(name)
        return port

    clock = iter(i * 0.25 for i in range(1000))
    monkeypatch.setattr(wcon, "open_port", fake_open)
    monkeypatch.setattr(wcon.time, "monotonic", lambda: next(clock))
    assert wcon.main(["--port", "COM35", "--listen", "5"]) == 0
    out = capsys.readouterr().out
    assert "port gone" not in out and opens == ["COM35"] and port.closed == 1


def test_listen_reopens_a_vanished_posix_port(monkeypatch, capsys, tmp_path):
    port = GonePort([b"I (1) a: b\n"])
    opens = []

    def fake_open(name, wait=15.0):
        opens.append(name)
        return port

    clock = iter(i * 0.25 for i in range(1000))
    monkeypatch.setattr(wcon, "open_port", fake_open)
    monkeypatch.setattr(wcon.time, "monotonic", lambda: next(clock))
    gone = str(tmp_path / "dev" / "ttyACM9")  # not there
    monkeypatch.setattr(wcon, "_is_device_path", lambda p: True)
    assert wcon.main(["--port", gone, "--listen", "2"]) == 0
    assert "port gone" in capsys.readouterr().out and len(opens) > 1
