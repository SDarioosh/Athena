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


# Odysseus after the module: `deploy` ends with deploy_odysseus; `odysseus` runs only it.

def fake_hub(tmp_path: Path, name: str = "fork_deploy.sh") -> tuple[Path, Path]:
    hub = tmp_path / "odysseus"
    (hub / "scripts").mkdir(parents=True)
    calls = tmp_path / "calls"
    script = hub / "scripts" / name
    script.write_text("#!/usr/bin/env bash\n"
                      f'echo "$* modules=[${{FORK_MODULES-unset}}] old=[${{BARVIS_MODULES-unset}}]'
                      f' running=${{FORK_DEPLOY_RUNNING-}}" >> {calls}\n')
    script.chmod(0o755)
    return hub, calls


def test_odysseus_deploy_runs_hub_script_without_modules(tmp_path):
    hub, calls = fake_hub(tmp_path)
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(hub))
    assert r.returncode == 0, r.stderr
    assert calls.read_text() == "deploy modules=[] old=[] running=1\n"
    assert f"updating Odysseus in {hub}" in r.stdout


def test_odysseus_deploy_falls_back_to_former_script_name(tmp_path):
    hub, calls = fake_hub(tmp_path, "barvis_deploy.sh")
    assert run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(hub)).returncode == 0
    assert calls.read_text().startswith("deploy ")


def test_odysseus_dir_found_from_running_container(tmp_path):
    hub, calls = fake_hub(tmp_path)
    docker = tmp_path / "docker"
    docker.write_text(f'#!/usr/bin/env bash\necho "{hub}"\n')
    docker.chmod(0o755)
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR="", DOCKER=str(docker))
    assert r.returncode == 0, r.stderr
    assert calls.exists()


def test_odysseus_skipped_when_off_or_started_by_the_hub(tmp_path):
    hub, calls = fake_hub(tmp_path)
    assert run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR="off").returncode == 0
    assert run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(hub),
               FORK_DEPLOY_RUNNING="1").returncode == 0
    assert not calls.exists()


def test_odysseus_not_found_is_a_hint_not_an_error():
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR="", DOCKER="false")
    assert r.returncode == 0
    assert "BARVIS_SOUND_ODYSSEUS_DIR" in r.stdout


def test_odysseus_dir_without_deploy_script_fails(tmp_path):
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(tmp_path))
    assert r.returncode == 1
    assert "no scripts/fork_deploy.sh" in r.stderr


def test_odysseus_deploy_failure_is_reported(tmp_path):
    hub, _ = fake_hub(tmp_path)
    (hub / "scripts" / "fork_deploy.sh").write_text("#!/usr/bin/env bash\nexit 3\n")
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(hub))
    assert r.returncode == 1
    assert "Odysseus deploy failed" in r.stderr


def test_odysseus_skipped_under_a_hub_script_without_the_marker(tmp_path):
    # hub scripts from before FORK_DEPLOY_RUNNING: recognized by name (here the
    # odysseus-deploy link the hub docs set up)
    hub, calls = fake_hub(tmp_path)
    caller = tmp_path / "odysseus-deploy"
    caller.write_text(f'#!/usr/bin/env bash\nbash "{SCRIPT}" odysseus\necho done\n')
    caller.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CONVO_", "BARVIS_SOUND_", "FORK_"))}
    r = subprocess.run([str(caller), "deploy"], capture_output=True, text=True, timeout=30,
                       env={**env, "BARVIS_SOUND_ODYSSEUS_DIR": str(hub)})
    assert r.returncode == 0, r.stderr
    assert not calls.exists()
