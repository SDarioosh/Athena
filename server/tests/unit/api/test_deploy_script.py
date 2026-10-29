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


def test_odysseus_deploy_runs_from_the_odysseus_checkout(tmp_path):
    # the first barvis_deploy.sh needs docker-compose.yml in the working directory
    hub, calls = fake_hub(tmp_path, "barvis_deploy.sh")
    (hub / "scripts" / "barvis_deploy.sh").write_text(
        f'#!/usr/bin/env bash\n[ -f docker-compose.yml ] || exit 7\npwd >> {calls}\n')
    (hub / "docker-compose.yml").write_text("services: {}\n")
    r = run("odysseus", BARVIS_SOUND_ODYSSEUS_DIR=str(hub))
    assert r.returncode == 0, r.stderr
    assert calls.read_text().strip() == str(hub)


# Docker runner: compose files and the checks before building.

def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n')
    docker.chmod(0o755)
    return docker, calls


def test_docker_gpu_api_adds_the_gpu_compose_file(tmp_path):
    docker, calls = fake_docker(tmp_path)
    assert run("units", BARVIS_SOUND_RUNNER="docker", DOCKER=str(docker),
               CONVO_DOCKER_API_GPU="True").returncode == 0
    assert f"-f {SERVER}/compose.yml -f {SERVER}/compose.gpu.yml config" in calls.read_text()


def test_docker_cpu_api_by_default(tmp_path):
    docker, calls = fake_docker(tmp_path)
    assert run("units", BARVIS_SOUND_RUNNER="docker", DOCKER=str(docker),
               CONVO_DOCKER_API_GPU="").returncode == 0
    assert "compose.gpu.yml" not in calls.read_text()


def test_docker_refuses_pyannote_without_its_extra(tmp_path):
    docker, calls = fake_docker(tmp_path)
    r = run("start", BARVIS_SOUND_RUNNER="docker", DOCKER=str(docker),
            CONVO_DIARIZER="pyannote", CONVO_DOCKER_EXTRAS="vad,diarization")
    assert r.returncode == 1
    assert "CONVO_DOCKER_EXTRAS=vad,diarization,pyannote" in r.stderr
    assert not calls.exists()  # nothing built or started


def test_docker_pyannote_with_its_extra_starts(tmp_path):
    docker, calls = fake_docker(tmp_path)
    run("start", BARVIS_SOUND_RUNNER="docker", DOCKER=str(docker),
        CONVO_DIARIZER="pyannote", CONVO_DOCKER_EXTRAS="vad,diarization,pyannote")
    assert " up -d --build" in calls.read_text()


# Meeting bot: BARVIS_SOUND_ZOOM_SDK_DIR unset finds ~/zoom-sdk; off turns the bot off.

def fake_sdk(home: Path) -> Path:
    sdk = home / "zoom-sdk"
    (sdk / "h").mkdir(parents=True)
    (sdk / "h" / "zoom_sdk.h").write_text("/* fictional */\n")
    return sdk


def docker_start(tmp_path: Path, home: Path, **env: str):
    docker, calls = fake_docker(tmp_path)
    base = {"BARVIS_SOUND_RUNNER": "docker", "DOCKER": str(docker), "HOME": str(home),
            "CONVO_ZOOM_SDK_KEY": "fictional-key", "CONVO_ZOOM_SDK_SECRET": "fictional-secret"}
    r = run("start", **{**base, **env})
    return r, calls.read_text() if calls.exists() else ""


def test_sdk_in_home_is_found_without_a_setting(tmp_path):
    home = tmp_path / "home"
    sdk = fake_sdk(home)
    r, calls = docker_start(tmp_path, home)
    assert f"-f {SERVER}/compose.meeting.yml" in calls
    assert f"found the Zoom Meeting SDK in {sdk}" in r.stdout
    assert r.stdout.count("found the Zoom Meeting SDK") == 1
    assert "CONVO_ZOOM_SDK_KEY" not in r.stdout  # keys set: no warning


def test_sdk_off_disables_the_bot(tmp_path):
    home = tmp_path / "home"
    fake_sdk(home)
    r, calls = docker_start(tmp_path, home, BARVIS_SOUND_ZOOM_SDK_DIR="off")
    assert " up -d --build" in calls and "compose.meeting.yml" not in calls
    assert "Zoom Meeting SDK" not in r.stdout


def test_sdk_empty_setting_still_means_no_bot(tmp_path):
    home = tmp_path / "home"
    fake_sdk(home)
    r, calls = docker_start(tmp_path, home, BARVIS_SOUND_ZOOM_SDK_DIR="")
    assert " up -d --build" in calls and "compose.meeting.yml" not in calls


def test_no_sdk_in_home_no_bot(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r, calls = docker_start(tmp_path, home)
    assert " up -d --build" in calls and "compose.meeting.yml" not in calls
    assert "Zoom Meeting SDK" not in r.stdout


def test_explicit_sdk_dir_wins_over_home(tmp_path):
    home = tmp_path / "home"
    fake_sdk(home)
    other = fake_sdk(tmp_path / "elsewhere")
    r, calls = docker_start(tmp_path, home, BARVIS_SOUND_ZOOM_SDK_DIR=str(other))
    assert "compose.meeting.yml" in calls and "found the Zoom Meeting SDK" not in r.stdout


def test_sdk_without_keys_warns_but_deploys(tmp_path):
    home = tmp_path / "home"
    fake_sdk(home)
    r, calls = docker_start(tmp_path, home, CONVO_ZOOM_SDK_KEY="", CONVO_ZOOM_SDK_SECRET="")
    assert r.returncode == 0, r.stderr
    assert "warning: CONVO_ZOOM_SDK_KEY / CONVO_ZOOM_SDK_SECRET are not set" in r.stdout
    assert "answers 501" in r.stdout and " up -d --build" in calls


def test_sudo_docker_gets_the_sdk_dir_by_name(tmp_path):
    """sudo drops the environment, so the auto-detected folder goes on its command line."""
    home = tmp_path / "home"
    sdk = fake_sdk(home)
    calls = tmp_path / "sudo-calls"
    sudo = tmp_path / "bin" / "sudo"
    sudo.parent.mkdir()
    sudo.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n')
    sudo.chmod(0o755)
    run("start", BARVIS_SOUND_RUNNER="docker", DOCKER=f"{sudo} docker", HOME=str(home),
        CONVO_ZOOM_SDK_KEY="k", CONVO_ZOOM_SDK_SECRET="s")
    first = calls.read_text().splitlines()[0]
    assert first.startswith(f"BARVIS_SOUND_ZOOM_SDK_DIR={sdk} docker compose ")
