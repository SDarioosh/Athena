"""Which hub addresses syncd talks to at all (pairing and every later request).

A pairing QR code names the server the device then sends its token and all its data to,
so a code held up to the camera must not be able to point syncd at this device itself or
at a cloud metadata service. Refused, as a URL's host:

- not `http`/`https` (plain http stays allowed: tailnet hubs are `http://<name>.ts.net`);
- loopback (`127.0.0.0/8`, `::1`, `localhost`, `*.localhost`), unspecified (`0.0.0.0`,
  `::`), link-local (`169.254.0.0/16` with the metadata address `169.254.169.254`,
  `fe80::/10`), multicast, the IPv4 broadcast and reserved blocks, the same as IPv4-mapped
  IPv6, and the cloud metadata hosts (`metadata.google.internal`, `metadata`,
  `instance-data`, `fd00:ec2::254`, `100.100.100.200`);
- an IPv4 address in a form the resolver reads as one (`2130706433`, `127.1`, `0x7f.1`):
  checked as that address.

Without a proxy, a host name is also resolved before each request and refused when any of
its addresses is one of these (a name that points at the device). With a proxy
(Tailscale's userspace mode) the proxy resolves it, and only the URL is checked.

`ATHENA_PDA_DEV_LOOPBACK_HUB=1` allows loopback (only loopback) for the tests and a hub
on the same PC (`system/dev/run-local.sh`); the image never sets it.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse

DEV_LOOPBACK_ENV = "ATHENA_PDA_DEV_LOOPBACK_HUB"

_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data",
          "instance-data.ec2.internal"}
_METADATA_IPS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
                 ipaddress.ip_address("100.100.100.200")}

Ip = ipaddress.IPv4Address | ipaddress.IPv6Address


class RefusedAddress(ValueError):
    """The hub's address is one syncd never talks to; the message is one short line."""


def _loopback_ok() -> bool:
    return os.environ.get(DEV_LOOPBACK_ENV) == "1"


def as_ip(host: str) -> Ip | None:
    """The address a host stands for when it is a literal (also legacy IPv4 forms)."""
    h = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if h and all(c in "0123456789abcdefxABCDEFX." for c in h) and h[0].isdigit():
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def refused_ip(ip: Ip) -> str | None:
    """Why this address is refused, or None."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA_IPS:
        return "a cloud metadata address"
    if ip.is_loopback:
        return None if _loopback_ok() else "this device itself"
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if isinstance(ip, ipaddress.IPv4Address) and (ip.is_reserved
                                                  or ip == ipaddress.IPv4Address("255.255.255.255")):
        return "a reserved address"
    return None


def refused_host(host: str) -> str | None:
    """Why this URL host is refused (without resolving it), or None."""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return "no host"
    ip = as_ip(h)
    if ip is not None:
        return refused_ip(ip)
    if h in _NAMES or h.endswith(".localhost"):
        if (h == "localhost" or h.endswith(".localhost")) and _loopback_ok():
            return None
        return "this device itself" if "localhost" in h else "a cloud metadata address"
    return None


def check_url(url: str) -> None:
    """Raise RefusedAddress unless `url` is http(s) on a host syncd may talk to."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RefusedAddress("Odysseus must be an http:// or https:// address.")
    why = refused_host(parts.hostname or "")
    if why:
        raise RefusedAddress(f"That address can't be Odysseus ({why}).")


def check_resolved(host: str, port: int | None) -> None:
    """Raise RefusedAddress when the host name resolves to a refused address. A name that
    doesn't resolve passes (the request itself then fails as unreachable)."""
    if as_ip(host) is not None:
        return
    try:
        infos = socket.getaddrinfo(host, port or 0, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return
    for info in infos:
        why = refused_ip(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        if why:
            raise RefusedAddress(f"That address can't be Odysseus ({host} is {why}).")
