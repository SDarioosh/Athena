"""Tests for the spike's SDK JWT signer (fictional key and secret only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPIKE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SPIKE))

import sign_jwt  # noqa: E402

KEY = "fictionalSdkKey123"
SECRET = "fictional-secret-not-real-0000"


def _decode(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_token_shape_and_signature():
    token = sign_jwt.sign(KEY, SECRET, now=1_800_000_000, seconds=3600)
    head, body, sig = token.split(".")
    assert _decode(head) == {"alg": "HS256", "typ": "JWT"}
    claims = _decode(body)
    assert claims["appKey"] == claims["sdkKey"] == KEY
    assert claims["iat"] == 1_800_000_000 - 30
    assert claims["exp"] == claims["tokenExp"] == claims["iat"] + 3600
    assert "mn" not in claims
    want = hmac.new(SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)) == want
    assert "=" not in token


def test_meeting_number_binds_participant_role():
    claims = _decode(sign_jwt.sign(KEY, SECRET, now=0, meeting_number="81234567890")
                     .split(".")[1])
    assert claims["mn"] == "81234567890"
    assert claims["role"] == 0


@pytest.mark.parametrize("seconds", [60, 49 * 3600])
def test_rejects_expiry_outside_zoom_limits(seconds):
    with pytest.raises(ValueError):
        sign_jwt.sign(KEY, SECRET, seconds=seconds)


def test_rejects_missing_credentials():
    with pytest.raises(ValueError):
        sign_jwt.sign("", SECRET)


def test_cli_reads_env_and_never_prints_secret():
    env = {"CONVO_ZOOM_SDK_KEY": KEY, "CONVO_ZOOM_SDK_SECRET": SECRET, "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, str(SPIKE / "sign_jwt.py"), "--hours", "1"],
                         env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out.count(".") == 2
    assert SECRET not in out


def test_cli_fails_cleanly_without_env():
    r = subprocess.run([sys.executable, str(SPIKE / "sign_jwt.py")],
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 2
    assert "CONVO_ZOOM_SDK_KEY" in r.stderr
