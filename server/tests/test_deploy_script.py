"""server/scripts/deploy.sh: syntax and the systemd units it writes (no services started)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
SERVER = SCRIPT.parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CONVO_", "BARVIS_SOUND_"))}
    # background runner: never touches the real systemd
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env={**base, "BARVIS_SOUND_RUNNER": "background", **env},
                          timeout=30)


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_units_follow_config():
    out = run("units", CONVO_MCP_TOKEN="x" * 20,
              BARVIS_SOUND_WHISPER_CMD="/opt/whisper-server -m m.bin --port 8178").stdout
    assert f"WorkingDirectory={SERVER}\n" in out
    assert f"ExecStart={SERVER}/.venv/bin/convo-server serve\n" in out
    assert f"ExecStart={SERVER}/.venv/bin/convo-server mcp --http\n" in out
    assert "ExecStart=/opt/whisper-server -m m.bin --port 8178\n" in out
    assert out.count("Restart=on-failure") == 3


def test_optional_services_off_without_config():
    # CONVO_MCP_TOKEN empty overrides any value in a local server/.env
    out = run("units", CONVO_MCP_TOKEN="", BARVIS_SOUND_WHISPER_CMD="").stdout
    assert "### barvis-sound.service" in out
    assert "barvis-sound-mcp" not in out and "barvis-sound-whisper" not in out


def test_usage_on_unknown_command():
    r = run("nope")
    assert r.returncode == 1 and "deploy.sh deploy" in r.stdout
