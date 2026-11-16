"""The hub addresses syncd refuses: at pairing, for the stored URL, and at every request."""

from __future__ import annotations

import json
import socket
import urllib.error

import _syncd_path  # noqa: F401 (the import path)
import pytest
from athena_pda_sync import addresses as ad
from athena_pda_sync import hub as h
from athena_pda_sync import pairing as pr
from syncd_fakes import CODE, HUB_TOKEN

REFUSED = [
    "http://127.0.0.1:7000", "http://127.9.9.9", "http://localhost:7000", "http://hub.localhost",
    "http://LOCALHOST.", "http://[::1]:7000", "http://0.0.0.0:7000", "http://[::]",
    "http://169.254.169.254", "http://169.254.1.2:7000", "http://[fe80::1]", "http://224.0.0.1",
    "http://255.255.255.255", "http://240.0.0.1", "http://metadata.google.internal",
    "http://metadata", "http://instance-data", "http://100.100.100.200",
    "http://[fd00:ec2::254]", "http://[::ffff:127.0.0.1]", "http://[::ffff:169.254.169.254]",
    "http://2130706433", "http://127.1", "http://0x7f.0.0.1", "http://017700000001",
]
ALLOWED = [
    "http://100.64.0.7:7000", "https://hub.example.ts.net", "http://hub.tail0000.ts.net:7000",
    "http://192.168.1.5:7000", "http://10.0.0.2", "http://[fd7a:115c:a1e0::1]:7000",
    "http://100.100.100.100",  # Tailscale's own resolver address is no metadata service
]


@pytest.fixture
def no_loopback(monkeypatch):
    monkeypatch.delenv(ad.DEV_LOOPBACK_ENV, raising=False)


@pytest.mark.parametrize("url", REFUSED)
def test_refused_addresses(no_loopback, url: str) -> None:
    with pytest.raises(ad.RefusedAddress):
        ad.check_url(url)
    with pytest.raises(pr.PairError, match="can't be Odysseus"):
        pr.check_url(url)


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_addresses(no_loopback, url: str) -> None:
    ad.check_url(url)
    assert pr.check_url(url) == url


def test_schemes(no_loopback) -> None:
    for url in ("ftp://hub.example.ts.net", "file:///etc/passwd", "gopher://hub.example"):
        with pytest.raises(ad.RefusedAddress):
            ad.check_url(url)
        with pytest.raises(pr.PairError):
            pr.check_url(url)


def test_dev_loopback_allows_only_loopback(monkeypatch) -> None:
    monkeypatch.setenv(ad.DEV_LOOPBACK_ENV, "1")
    ad.check_url("http://127.0.0.1:7010")
    ad.check_url("http://localhost:7010")
    for url in ("http://169.254.169.254", "http://0.0.0.0", "http://metadata.google.internal"):
        with pytest.raises(ad.RefusedAddress):
            ad.check_url(url)


def test_a_pairing_qr_code_for_a_refused_host(no_loopback) -> None:
    link = f"odysseus-pair:v1?url=http%3A%2F%2F169.254.169.254&code={CODE}"
    with pytest.raises(pr.PairError, match="can't be Odysseus"):
        pr.parse_link(link)
    with pytest.raises(pr.PairError, match="can't be Odysseus"):
        pr.redeem("http://localhost:7000", CODE)  # nothing is sent


def test_a_stored_refused_url_is_unpaired(no_loopback, tmp_path) -> None:
    path = tmp_path / "token"
    path.write_text(json.dumps({"url": "http://127.0.0.1:7000", "token": HUB_TOKEN}))
    path.chmod(0o600)
    assert pr.TokenStore(path).load() is None
    with pytest.raises(ValueError):
        h.Hub.make("http://127.0.0.1:7000", HUB_TOKEN)


def test_every_request_is_checked_also_after_dns(no_loopback, monkeypatch) -> None:
    """A name that resolves to this device is refused before anything is sent."""
    connects: list[object] = []
    real = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *a, **kw):
        if host == "evil.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real(host, port, *a, **kw)

    def no_connect(*a, **kw):
        connects.append(a)
        raise AssertionError("connected")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", no_connect)
    with pytest.raises(urllib.error.URLError, match="evil.example is this device itself"):
        h.make_opener(None).open("http://evil.example:7000/api/codex/athena/health", timeout=2)
    with pytest.raises(urllib.error.URLError, match="can't be Odysseus"):
        h.make_opener(None).open("http://169.254.169.254/latest/meta-data", timeout=2)
    with pytest.raises(urllib.error.URLError, match="can't be Odysseus"):
        h.make_opener("http://127.0.0.1:1055").open("http://metadata.google.internal/", timeout=2)
    assert connects == []
