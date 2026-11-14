"""Frames of glance.sock, the pairing link, redeeming a code and the token file."""

from __future__ import annotations

import json
import os
import stat

import _syncd_path  # noqa: F401 (the import path)
import pytest
from athena_pda_sync import frames as fr
from athena_pda_sync import pairing as pr
from syncd_fakes import CODE, HUB_TOKEN, FakeHub


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


# ----------------------------------------------------------------- frames


def test_frame_bytes() -> None:
    assert fr.encode(fr.CTRL, b"\x05") == b"\x01\x01\x00\x05"
    assert fr.encode(fr.STATUS_REQ) == b"\x03\x00\x00"
    assert fr.decode_header(b"\x02\x00\x02") == (fr.DATA, 512)
    assert fr.encode(fr.CTRL, b"x" * 512)[:3] == b"\x01\x00\x02"
    with pytest.raises(fr.FrameError):
        fr.encode(fr.CTRL, b"x" * 513)
    with pytest.raises(fr.FrameError):
        fr.decode_header(b"\x02\x01\x02")  # 513


def test_local_frames() -> None:
    frame = fr.encode_local({"t": "net", "msg": "Grüße"})
    assert frame[0] == fr.LOCAL
    assert fr.decode_local(frame[3:]) == {"t": "net", "msg": "Grüße"}
    assert "Grüße".encode() in frame  # UTF-8, not \u escapes
    assert fr.decode_local(b"[1]") is None
    assert fr.decode_local(b"\xff") is None
    with pytest.raises(fr.FrameError):
        fr.encode_local({"t": "net", "msg": "x" * 600})


# ----------------------------------------------------------------- the pairing link


def test_parse_link() -> None:
    url, code = pr.parse_link(
        "odysseus-pair:v1?url=https%3A%2F%2Fody.tail1234.ts.net%3A7000&code=" + CODE)
    assert (url, code) == ("https://ody.tail1234.ts.net:7000", CODE)
    # plain URL, trailing slash, extra keys, first value wins, whitespace around
    url, code = pr.parse_link(f"  odysseus-pair:v1?x=1&url=http://100.64.0.7:7000/&code={CODE}"
                              f"&code=other  ")
    assert (url, code) == ("http://100.64.0.7:7000", CODE)


@pytest.mark.parametrize("raw, why", [
    ("https://ody.example/pair", "Not an Odysseus pairing code"),
    ("odysseus-pair:v2?url=http://h&code=" + CODE, "Not an Odysseus pairing code"),
    ("odysseus-pair:v1?url=http://h", "no valid code"),
    ("odysseus-pair:v1?url=http://h&code=short", "no valid code"),
    ("odysseus-pair:v1?url=http://h&code=bad%20code%20here", "no valid code"),
    ("odysseus-pair:v1?url=ftp://h&code=" + CODE, "Odysseus address in the code"),
    ("odysseus-pair:v1?url=http%3A%2F%2Fu%3Ap%40h&code=" + CODE, "can't have a user"),
    ("odysseus-pair:v1?url=http%3A%2F%2Fh%2F%3Fq%3D1&code=" + CODE, "can't have a user"),
    ("odysseus-pair:v1?url=%FF&code=" + CODE, "damaged"),
])
def test_parse_link_refuses(raw: str, why: str) -> None:
    with pytest.raises(pr.PairError, match=why):
        pr.parse_link(raw)


def test_check_url_and_code() -> None:
    assert pr.check_url(" http://ody:7000/ ") == "http://ody:7000"
    for bad in ("", "ody:7000", "http://", "http://h:99999", "http://h#x", "http://a b"):
        with pytest.raises(pr.PairError):
            pr.check_url(bad)
    assert pr.check_code(f" {CODE} ") == CODE
    with pytest.raises(pr.PairError):
        pr.check_code("abc")


# ----------------------------------------------------------------- redeem


def test_redeem_ok(hub: FakeHub) -> None:
    paired = pr.redeem(hub.url + "/", CODE)
    assert paired.url == hub.url and paired.token == HUB_TOKEN
    assert paired.host == "127.0.0.1"
    assert HUB_TOKEN not in repr(paired)
    method, path, headers, body = hub.requests[-1]
    assert (method, path, body) == ("POST", "/api/pair/redeem", {"code": CODE})
    assert "Authorization" not in headers


def test_redeem_used_expired_and_limited(hub: FakeHub) -> None:
    pr.redeem(hub.url, CODE)
    with pytest.raises(pr.PairError, match="not valid any more"):
        pr.redeem(hub.url, CODE)  # single use
    hub.codes["Expired_code_0001"] = 0.0
    with pytest.raises(pr.PairError, match="not valid any more"):
        pr.redeem(hub.url, "Expired_code_0001")
    hub.rate_limited = True
    with pytest.raises(pr.PairError, match="Too many tries"):
        pr.redeem(hub.url, "Another_code_0002")


def test_redeem_unreachable_and_old_hub(hub: FakeHub, monkeypatch) -> None:
    hub.drop = True
    with pytest.raises(pr.PairError, match="not reachable at 127.0.0.1"):
        pr.redeem(hub.url, CODE, timeout=3)
    hub.drop = False
    monkeypatch.setattr(hub, "_redeem", lambda body: (404, {"detail": "Not Found"}))
    with pytest.raises(pr.PairError, match="can't pair devices yet"):
        pr.redeem(hub.url, CODE)
    monkeypatch.setattr(hub, "_redeem", lambda body: (500, {"detail": "boom"}))
    with pytest.raises(pr.PairError, match="HTTP 500"):
        pr.redeem(hub.url, CODE)


def test_redeem_without_token(hub: FakeHub, monkeypatch) -> None:
    monkeypatch.setattr(hub, "_redeem", lambda body: (200, {"token": "not-an-ody-token"}))
    with pytest.raises(pr.PairError, match="without a token"):
        pr.redeem(hub.url, CODE)


# ----------------------------------------------------------------- token file


def test_token_file(tmp_path) -> None:
    os.umask(0o022)
    store = pr.TokenStore(tmp_path / "sub" / "token")
    assert store.load() is None
    store.save(pr.Paired("http://ody:7000", HUB_TOKEN, "Phone relay"))
    st = (tmp_path / "sub" / "token").stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert json.loads((tmp_path / "sub" / "token").read_text()) == {
        "url": "http://ody:7000", "token": HUB_TOKEN, "name": "Phone relay"}
    assert not list((tmp_path / "sub").glob("*.tmp"))
    assert store.load() == pr.Paired("http://ody:7000", HUB_TOKEN, "Phone relay")
    # a second save replaces it, still 0600
    store.save(pr.Paired("http://ody2:7000", HUB_TOKEN))
    assert store.load().url == "http://ody2:7000"
    assert stat.S_IMODE((tmp_path / "sub" / "token").stat().st_mode) == 0o600
    assert store.delete() is True
    assert store.load() is None and store.delete() is False


def test_token_file_damaged_or_open(tmp_path, caplog) -> None:
    path = tmp_path / "token"
    path.write_text("{not json")
    assert pr.TokenStore(path).load() is None
    path.write_text(json.dumps({"url": "http://ody", "token": HUB_TOKEN}))
    os.chmod(path, 0o644)
    assert pr.TokenStore(path).load().token == HUB_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert HUB_TOKEN not in caplog.text


# ----------------------------------------------------------------- --pair


def test_cli_pair_reads_the_link_from_stdin(hub: FakeHub, tmp_path, monkeypatch) -> None:
    """The code never goes in a command line (`ps` shows every user's): --pair takes no
    value and reads the link from stdin."""
    import io

    from athena_pda_sync.__main__ import main

    monkeypatch.setenv("ATHENA_PDA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ATHENA_PDA_RUN", str(tmp_path / "r"))
    quoted = hub.url.replace(":", "%3A").replace("/", "%2F")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"odysseus-pair:v1?url={quoted}&code={CODE}\n"))
    assert main(["--pair"]) == 0
    assert json.loads((tmp_path / "d" / "token").read_text())["token"] == HUB_TOKEN
    with pytest.raises(SystemExit):
        main(["--pair", f"odysseus-pair:v1?url={quoted}&code={CODE}"])  # no value taken
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--pair"]) == 3  # nothing on stdin: not a pairing link
