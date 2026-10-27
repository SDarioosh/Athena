#!/usr/bin/env python3
"""Sign a Zoom Meeting SDK JWT locally (Phase 0 spike; Python stdlib only).

Reads the Meeting SDK app's key and secret from the environment
(`CONVO_ZOOM_SDK_KEY`, `CONVO_ZOOM_SDK_SECRET`) and prints an HS256 JWT on stdout,
which the spike bots pass to `IAuthService::SDKAuth`. Nothing leaves the machine.

Claims follow Zoom's Meeting SDK auth docs as we know them (`appKey`, `sdkKey`, `iat`,
`exp`, `tokenExp`; `mn` and `role` only when a meeting number is given). Zoom accepts
an expiry of 30 minutes to 48 hours after `iat`. Check the current docs if SDKAuth
answers `AUTHRET_JWTTOKENWRONG`.

Usage:
    CONVO_ZOOM_SDK_KEY=... CONVO_ZOOM_SDK_SECRET=... python3 sign_jwt.py [--meeting 123] [--hours 2]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time

MIN_SECONDS = 30 * 60
MAX_SECONDS = 48 * 3600


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(key: str, secret: str, *, now: int | None = None, seconds: int = 2 * 3600,
         meeting_number: str | None = None, role: int = 0) -> str:
    """Return an HS256 JWT for SDKAuth. `role` 0 = participant (the bot never hosts)."""
    if not key or not secret:
        raise ValueError("SDK key and secret must not be empty")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ValueError(f"expiry must be {MIN_SECONDS}..{MAX_SECONDS} seconds")
    iat = int(time.time() if now is None else now) - 30  # small clock-skew margin
    exp = iat + seconds
    payload: dict[str, object] = {"appKey": key, "sdkKey": key, "iat": iat, "exp": exp,
                                  "tokenExp": exp}
    if meeting_number:
        payload["mn"] = str(meeting_number)
        payload["role"] = role
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode())
        for part in (header, payload))
    sig = hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--meeting", help="meeting number to bind the token to (optional)")
    ap.add_argument("--hours", type=float, default=2.0, help="validity, 0.5..48 (default 2)")
    args = ap.parse_args(argv)
    key = os.environ.get("CONVO_ZOOM_SDK_KEY", "")
    secret = os.environ.get("CONVO_ZOOM_SDK_SECRET", "")
    try:
        print(sign(key, secret, seconds=int(args.hours * 3600), meeting_number=args.meeting))
    except ValueError as e:
        print(f"sign_jwt: {e} (set CONVO_ZOOM_SDK_KEY and CONVO_ZOOM_SDK_SECRET)",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
