"""server/scripts/deploy.sh: syntax and the systemd units it writes (no services started)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy.sh"
SERVER = SCRIPT.parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


_NO_API_BIN: Path | None = None


def _no_api_bin() -> Path:
    """A bin dir whose curl never reaches anything, so a test never asks a real Athena on
    this machine whether a meeting is running (a developer's server/.env has a real token),
    and whose systemctl and docker do nothing, so a test never stops a real service or
    container (on the owner's server the real ones run Athena)."""
    global _NO_API_BIN
    if _NO_API_BIN is None:
        _NO_API_BIN = Path(tempfile.mkdtemp(prefix="deploy-test-bin-"))
        for name, body in (("curl", "exit 7"), ("systemctl", "exit 1"), ("docker", "exit 0"),
                           ("journalctl", "exit 0")):
            tool = _NO_API_BIN / name
            tool.write_text(f"#!/usr/bin/env bash\n{body}\n")
            tool.chmod(0o755)
        # no unit files and no pid files of a real install
        (_NO_API_BIN / "cfg").mkdir()
        (_NO_API_BIN / "data").mkdir()
    return _NO_API_BIN


def run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CONVO_", "BARVIS_SOUND_", "ATHENA_", "FORK_", "DOCKER",
                                 "COMPOSE_"))}
    # tests that fake their own tools pass PATH; the others get a curl that can't connect
    base["PATH"] = f"{_no_api_bin()}:{base.get('PATH', '')}"
    base["XDG_CONFIG_HOME"] = str(_no_api_bin() / "cfg")
    base["CONVO_DATA_DIR"] = str(_no_api_bin() / "data")
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


# A meeting in progress: deploy, start and restart refuse while GET /v1/meetings lists an
# active bot, unless --force (or BARVIS_SOUND_FORCE=1). Fakes: curl (the API), systemctl
# and loginctl (systemd), docker.

INGEST = "fictional-ingest-token-0123456789"
ACTIVE = ('{"meetings": [{"id": "m1", "state": "recording", "active": true, '
          '"meeting_id": "81234567890", "title": "Budget  sync (fictional)"}, '
          '{"id": "m0", "state": "ended", "active": false, "meeting_id": "123456789", '
          '"title": null}]}')
NONE_ACTIVE = '{"meetings": [{"id": "m0", "state": "ended", "active": false}]}'


def fake_system(tmp_path: Path, body: str | None = ACTIVE, code: str = "200") -> dict:
    """A bin dir with fake curl, systemctl and loginctl; the env that uses it. ``body``
    None: the API doesn't answer."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "log"
    (tmp_path / "body.json").write_text(body or "")
    (bin_dir / "curl").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        url="${{@: -1}}"
        case $url in
          */v1/meetings*)
            echo "curl $*" >> {log}
            echo "stdin $(cat)" >> {log}
            {"exit 7" if body is None else ""}
            cat {tmp_path / "body.json"}; printf '\\n%s' {code} ;;
          *) exit 0 ;;  # /health
        esac
        """))
    (bin_dir / "systemctl").write_text(f'#!/usr/bin/env bash\necho "systemctl $*" >> {log}\n')
    (bin_dir / "loginctl").write_text("#!/usr/bin/env bash\necho yes\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "CONVO_INGEST_TOKEN": INGEST, "CONVO_MCP_TOKEN": "", "CONVO_PORT": "18765",
            "BARVIS_SOUND_WHISPER_CMD": "", "BARVIS_SOUND_RUNNER": "systemd"}


def calls(tmp_path: Path) -> str:
    log = tmp_path / "log"
    return log.read_text() if log.exists() else ""


def test_restart_refused_while_a_bot_is_active(tmp_path):
    r = run("restart", **fake_system(tmp_path))
    assert r.returncode == 1
    assert "Athena is in a meeting right now" in r.stderr
    assert '"Budget sync (fictional)" (Zoom meeting 81234567890, recording, id m1)' in r.stderr
    assert "m0" not in r.stderr  # an ended meeting is not named
    assert "deploy.sh restart --force" in r.stderr and "BARVIS_SOUND_FORCE=1" in r.stderr
    assert "systemctl" not in calls(tmp_path)  # nothing stopped or restarted
    # the API is asked with the ingest token, which never goes on curl's command line
    assert "http://127.0.0.1:18765/v1/meetings?limit=100" in calls(tmp_path)
    assert f"stdin Authorization: Bearer {INGEST}" in calls(tmp_path)
    assert INGEST not in "".join(line for line in calls(tmp_path).splitlines(True)
                                 if line.startswith("curl "))


def test_restart_with_force_restarts_and_names_the_meeting(tmp_path):
    for env in ({}, {"BARVIS_SOUND_FORCE": "1"}):
        args = ("restart",) if env else ("restart", "--force")
        r = run(*args, **{**fake_system(tmp_path), **env})
        assert r.returncode == 0, r.stderr
        assert "--force: restarting Athena's API although a meeting bot is active" in r.stdout
        assert "Budget sync (fictional)" in r.stdout
        assert "systemctl --user restart barvis-sound" in calls(tmp_path)
        (tmp_path / "log").unlink()


def test_restart_without_an_active_bot(tmp_path):
    r = run("restart", **fake_system(tmp_path, NONE_ACTIVE))
    assert r.returncode == 0, r.stderr
    assert "meeting" not in r.stdout + r.stderr
    assert "systemctl --user restart barvis-sound" in calls(tmp_path)


@pytest.mark.parametrize("body, code, note", [
    (None, "200", "Athena's API is not answering on http://127.0.0.1:18765/v1/meetings"),
    ('{"detail": "unauthorized"}', "401", "answered HTTP 401"),
    ("not json", "200", "could not read the meetings list"),
])
def test_restart_goes_on_when_the_api_cannot_tell(tmp_path, body, code, note):
    r = run("restart", **fake_system(tmp_path, body, code))
    assert r.returncode == 0, r.stderr
    assert "note: could not check for a meeting in progress" in r.stdout and note in r.stdout
    assert "systemctl --user restart barvis-sound" in calls(tmp_path)


def test_no_token_goes_on_with_a_note(tmp_path):
    r = run("restart", **{**fake_system(tmp_path), "CONVO_INGEST_TOKEN": ""})
    assert r.returncode == 0, r.stderr
    assert "no CONVO_INGEST_TOKEN" in r.stdout
    assert "/v1/meetings" not in calls(tmp_path)


def test_docker_runner_refuses_and_forces(tmp_path):
    docker, docker_calls = fake_docker(tmp_path)
    env = {**fake_system(tmp_path), "BARVIS_SOUND_RUNNER": "docker", "DOCKER": str(docker),
           "BARVIS_SOUND_ZOOM_SDK_DIR": "off"}
    for cmd in ("start", "restart"):
        r = run(cmd, **env)
        assert r.returncode == 1 and f"not running '{cmd}'" in r.stderr
        assert not docker_calls.exists()
    assert " restart" in (run("restart", "--force", **env), docker_calls.read_text())[1]
    assert " up -d --build" in (run("start", "--force", **env), docker_calls.read_text())[1]


def test_docker_runner_without_an_active_bot(tmp_path):
    docker, docker_calls = fake_docker(tmp_path)
    r = run("restart", **{**fake_system(tmp_path, NONE_ACTIVE), "BARVIS_SOUND_RUNNER": "docker",
                          "DOCKER": str(docker)})
    assert r.returncode == 0, r.stderr
    assert " restart" in docker_calls.read_text()


def test_deploy_refused_before_the_checkout_moves(tmp_path):
    head = subprocess.run(["git", "-C", str(SERVER), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout
    # a remote that doesn't exist: if the check let it through, the fetch would fail
    env = {**fake_system(tmp_path), "BARVIS_SOUND_REMOTE": "no-such-remote-for-tests"}
    r = run("deploy", **env)
    assert r.returncode == 1 and "not running 'deploy'" in r.stderr
    assert "deploy.sh deploy --force" in r.stderr
    assert "fetching" not in r.stdout and "systemctl" not in calls(tmp_path)
    assert subprocess.run(["git", "-C", str(SERVER), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == head
    # forced, or with the API down, it goes on to the next step (here: the bad remote)
    r = run("deploy", "--force", **env)
    assert "warning: --force" in r.stdout and "not running" not in r.stderr
    r = run("deploy", **{**fake_system(tmp_path, None), "BARVIS_SOUND_REMOTE":
                         "no-such-remote-for-tests"})
    assert "note: could not check for a meeting in progress" in r.stdout
    assert "not running" not in r.stderr


def test_force_is_never_read_from_dotenv():
    text = SCRIPT.read_text()
    assert "env_value BARVIS_SOUND_FORCE" not in text
    assert 'if [ "${BARVIS_SOUND_FORCE:-}" = 1 ]; then FORCE=1; fi' in text


def test_help_documents_force():
    r = run("--help")
    assert r.returncode == 0
    assert "deploy.sh deploy" in r.stdout and "--force" in r.stdout
    assert "BARVIS_SOUND_FORCE=1" in r.stdout


def test_hub_deploy_during_a_meeting_fails_fast(tmp_path):
    """The hub's fork_deploy.sh runs each module as ``FORK_DEPLOY_RUNNING=1 <deploy.sh>
    deploy < /dev/null`` (no flags) and reports a failed module at its end: the refusal
    must come at once (no prompt, no wait), and BARVIS_SOUND_FORCE=1 passes through."""
    hub = tmp_path / "fork_deploy.sh"
    hub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        FORK_DEPLOY_RUNNING=1 bash {SCRIPT} deploy < /dev/null || echo "module failed"
        """))
    hub.chmod(0o755)
    env = {**{k: v for k, v in os.environ.items()
              if not k.startswith(("CONVO_", "BARVIS_SOUND_", "FORK_"))},
           **fake_system(tmp_path), "BARVIS_SOUND_REMOTE": "no-such-remote-for-tests"}
    r = subprocess.run([str(hub)], capture_output=True, text=True, timeout=30, env=env)
    assert "module failed" in r.stdout and "not running 'deploy'" in r.stderr
    assert "BARVIS_SOUND_FORCE=1 scripts/fork_deploy.sh deploy" in r.stderr
    r = subprocess.run([str(hub)], capture_output=True, text=True, timeout=30,
                       env={**env, "BARVIS_SOUND_FORCE": "1"})
    assert "warning: --force" in r.stdout and "not running" not in r.stderr
