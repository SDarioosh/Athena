"""Tests for pull.py's output checks (no serial port needed; pyserial is stubbed)."""

from __future__ import annotations

import sys
import types

import pytest

sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))
import pull  # noqa: E402


class FakePort:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["pull.py", "--port", "COM35", *argv])
    return pull.main()


def test_out_inside_the_repository_is_refused(monkeypatch):
    out = pull.REPO_ROOT / "firmware" / "recorder" / "pulled-test-dir"
    monkeypatch.setattr(pull, "open_port", lambda port: pytest.fail("port opened"))
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--out", str(out))
    assert not out.exists()


def test_safe_name_matches_the_firmware_get_check():
    for ok in ("rec_unset_8.wav", "q/0000002a.opus", "a" * 64):
        assert pull.safe_name(ok), ok
    for bad in ("", "../x.wav", "q/../x.opus", "/etc/x.wav", "sub/x.wav", "C:x.wav",
                "a\\b.wav", ".hidden.wav", "q/", "q/.x", "a" * 65):
        assert not pull.safe_name(bad), bad


def test_unsafe_names_are_never_written(monkeypatch, tmp_path):
    out = tmp_path / "rec"
    monkeypatch.setattr(pull, "open_port", lambda port: FakePort())
    monkeypatch.setattr(pull, "list_card", lambda s, queue=False: {
        "../escape.wav": 3, "ok.wav": 3})
    monkeypatch.setattr(pull, "get_file", lambda s, name, cmd=None: b"abc")
    assert run_main(monkeypatch, "--out", str(out)) == 1  # one name failed
    assert (out / "ok.wav").read_bytes() == b"abc"
    assert not (tmp_path / "escape.wav").exists()
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["ok.wav", "rec"]
