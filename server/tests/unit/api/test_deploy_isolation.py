"""server/scripts/deploy.sh and compose.yml on a host where Athena runs as its own user:
a root-only secrets file, masked `units`, the database copy before a deploy, the fixed
network. docker and curl are fakes; nothing is started."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy.sh"
SERVER = SCRIPT.parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

INGEST = "fictional-ingest-token-0123456789"
MCP = "fictional-mcp-token-9876543210"
ACTIVE = ('{"meetings": [{"id": "m1", "state": "recording", "active": true, '
          '"meeting_id": "81234567890", "title": "Budget sync (fictional)"}]}')
CONFIG = textwrap.dedent(f"""\
    services:
      sound-mcp:
        environment:
          CONVO_MCP_TOKEN: {MCP}
          CONVO_ZOOM_SDK_KEY: fictional-key-value
          CONVO_ZOOM_CLIENT_SECRET: "fictional secret value"
          ATHENA_INGEST_TOKEN: '{INGEST}'
          CONVO_LLM_MODEL: qwen3.5-35b
          CONVO_MCP_PORT: "8001"
    """)


def tools(tmp: Path, docker_body: str = "") -> tuple[dict, Path]:
    """Fake docker and curl that log their calls; the env to run the script with."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "log"
    (bin_dir / "docker").write_text(
        f'#!/usr/bin/env bash\necho "docker $*" >> {log}\n{docker_body}\nexit 0\n')
    (bin_dir / "curl").write_text(f'#!/usr/bin/env bash\necho "curl $*" >> {log}\nexit 7\n')
    (bin_dir / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp / "data").mkdir()
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CONVO_", "BARVIS_SOUND_", "ATHENA_", "FORK_", "DOCKER",
                                 "COMPOSE_"))}
    env = {**base, "PATH": f"{bin_dir}:{base['PATH']}", "XDG_CONFIG_HOME": str(tmp / "cfg"),
           "CONVO_DATA_DIR": str(tmp / "data"), "DOCKER": str(bin_dir / "docker"),
           "CONVO_PORT": "18765", "ATHENA_ZOOM_SDK_DIR": "off",
           "ATHENA_SECRETS_FILE": str(tmp / "no-secrets.env"),
           "CONVO_INGEST_TOKEN": "", "CONVO_MCP_TOKEN": ""}
    return env, log


def run(env: dict, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=120)


def logged(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def secrets(tmp: Path, text: str = f"CONVO_INGEST_TOKEN={INGEST}\n") -> Path:
    f = tmp / "secrets.env"
    f.write_text(text)
    f.chmod(0o600)
    return f


# --- units never prints a secret -------------------------------------------------------

def test_units_masks_tokens_keys_and_secrets(tmp_path):
    (tmp_path / "config.yml").write_text(CONFIG)
    env, _ = tools(tmp_path, f'case " $* " in *" config "*) cat {tmp_path / "config.yml"} ;; esac')
    r = run(env, "units")
    assert r.returncode == 0, r.stderr
    for value in (MCP, INGEST, "fictional-key-value", "fictional secret value"):
        assert value not in r.stdout
    assert 'CONVO_MCP_TOKEN: "***"' in r.stdout and 'CONVO_ZOOM_SDK_KEY: "***"' in r.stdout
    # everything else is printed as compose says it
    assert "CONVO_LLM_MODEL: qwen3.5-35b" in r.stdout and 'CONVO_MCP_PORT: "8001"' in r.stdout


# --- the meeting check with the token in the secrets file ------------------------------

def test_meeting_check_asks_inside_sound_api_when_the_token_is_in_the_secrets_file(tmp_path):
    (tmp_path / "body.json").write_text(ACTIVE)
    env, log = tools(tmp_path, textwrap.dedent(f"""\
        case " $* " in
          *" exec -T sound-api python "*) cat {tmp_path / "body.json"}; printf '\\n200' ;;
        esac"""))
    env["ATHENA_SECRETS_FILE"] = str(secrets(tmp_path))
    r = run(env, "restart")
    assert r.returncode == 1
    assert "Athena is in a meeting right now" in r.stderr
    assert '"Budget sync (fictional)"' in r.stderr
    assert "exec -T sound-api python -c" in logged(log)
    assert "curl" not in logged(log)          # the host never asks with a token
    assert INGEST not in logged(log)          # and never handles it
    assert " restart" not in logged(log).replace("restart --force", "")


def test_meeting_check_inside_a_stopped_container_goes_on_with_a_note(tmp_path):
    env, log = tools(tmp_path, 'case " $* " in *" exec "*) exit 1 ;; esac')
    env["ATHENA_SECRETS_FILE"] = str(secrets(tmp_path))
    r = run(env, "restart")
    assert r.returncode == 0, r.stderr
    assert "could not check for a meeting in progress" in r.stdout
    assert "not answering in sound-api" in r.stdout
    assert "compose.yml restart" in logged(log)


def test_without_a_secrets_file_and_a_token_the_note_stays(tmp_path):
    env, log = tools(tmp_path)
    r = run(env, "restart")
    assert r.returncode == 0, r.stderr
    assert "no CONVO_INGEST_TOKEN" in r.stdout
    assert " exec " not in logged(log)


# --- warnings that read the secrets file by name only ----------------------------------

def test_zoom_keys_in_the_secrets_file_count_as_set(tmp_path):
    sdk = tmp_path / "zoom-sdk"
    (sdk / "h").mkdir(parents=True)
    (sdk / "h" / "zoom_sdk.h").write_text("/* fictional */\n")
    env, _ = tools(tmp_path)
    env.update(ATHENA_ZOOM_SDK_DIR=str(sdk), BARVIS_SOUND_RUNNER="docker",
               ATHENA_SECRETS_FILE=str(secrets(
                   tmp_path, "CONVO_ZOOM_SDK_KEY=k\nCONVO_ZOOM_SDK_SECRET=s\n")))
    r = run(env, "start")
    assert "CONVO_ZOOM_SDK_SECRET are not set" not in r.stdout
    (tmp_path / "secrets.env").write_text("CONVO_ZOOM_SDK_KEY=k\n")
    r = run(env, "start")
    assert "CONVO_ZOOM_SDK_SECRET are not set" in r.stdout


def test_missing_mcp_token_is_a_warning_and_the_secrets_file_counts(tmp_path):
    env, _ = tools(tmp_path)
    env["BARVIS_SOUND_RUNNER"] = "docker"
    r = run(env, "start")
    assert "CONVO_MCP_TOKEN is set neither" in r.stdout
    env["ATHENA_SECRETS_FILE"] = str(secrets(tmp_path, f"CONVO_MCP_TOKEN={MCP}\n"))
    r = run(env, "start")
    assert "CONVO_MCP_TOKEN is set neither" not in r.stdout
    assert MCP not in r.stdout + r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_secrets_file_is_never_guessed_about(tmp_path):
    env, _ = tools(tmp_path)
    f = secrets(tmp_path, "")
    f.chmod(0)
    env.update(BARVIS_SOUND_RUNNER="docker", ATHENA_SECRETS_FILE=str(f))
    r = run(env, "start")
    assert "CONVO_MCP_TOKEN is set neither" not in r.stdout


def test_sudo_docker_gets_settings_from_the_environment_by_name(tmp_path):
    env, log = tools(tmp_path)
    sudo = Path(env["DOCKER"]).parent / "sudo"
    sudo.write_text(f'#!/usr/bin/env bash\necho "sudo $*" >> {log}\n')
    sudo.chmod(0o755)
    env.update(DOCKER="sudo docker", ATHENA_NET_SUBNET="172.29.10.0/24")
    assert run(env, "units").returncode == 0
    line = next(x for x in logged(log).splitlines() if " config" in x)
    assert "ATHENA_NET_SUBNET=172.29.10.0/24" in line
    assert f"ATHENA_SECRETS_FILE={tmp_path / 'no-secrets.env'}" in line
    assert "-f " + str(SERVER / "compose.network.yml") in line


# --- the fixed network -----------------------------------------------------------------

def test_a_subnet_adds_the_network_file(tmp_path):
    env, log = tools(tmp_path)
    assert run(env, "units").returncode == 0
    assert "compose.network.yml" not in logged(log)
    log.unlink()
    assert run({**env, "ATHENA_NET_SUBNET": "172.29.10.0/24"}, "units").returncode == 0
    assert f"-f {SERVER}/compose.yml -f {SERVER}/compose.network.yml config" in logged(log)


# --- the database copy before a deploy -------------------------------------------------

def test_deploy_copies_the_database_before_the_checkout_moves(tmp_path):
    env, log = tools(tmp_path)
    # a remote that doesn't exist: the deploy stops at the fetch, after the copy
    env.update(ATHENA_DB_BACKUPS="3", ATHENA_REMOTE="no-such-remote-for-tests")
    r = run(env, "deploy")
    assert r.returncode != 0
    calls = logged(log)
    assert "run --rm --no-deps -T --entrypoint python sound-api -c" in calls
    assert calls.rstrip().endswith(" 3")  # how many to keep


def test_a_failed_copy_stops_the_deploy_before_anything_changes(tmp_path):
    fail = 'case " $* " in *" run --rm "*) echo "disk full" >&2; exit 1 ;; esac'
    env, log = tools(tmp_path, fail)
    env.update(ATHENA_DB_BACKUPS="3", ATHENA_REMOTE="no-such-remote-for-tests")
    r = run(env, "deploy")
    assert r.returncode == 1
    assert "could not copy the database; nothing was changed" in r.stderr
    assert "disk full" in r.stderr
    assert "fetching" not in r.stdout


def test_no_copy_unless_asked_and_a_bad_count_is_an_error(tmp_path):
    env, log = tools(tmp_path)
    env["ATHENA_REMOTE"] = "no-such-remote-for-tests"
    run(env, "deploy")
    assert " run --rm" not in logged(log)
    r = run({**env, "ATHENA_DB_BACKUPS": "five"}, "deploy")
    assert r.returncode == 1 and "ATHENA_DB_BACKUPS must be a number" in r.stderr


# --- compose.yml ------------------------------------------------------------------------

def test_compose_reads_the_secrets_file_and_hardens_every_service():
    text = (SERVER / "compose.yml").read_text()
    assert "path: ${ATHENA_SECRETS_FILE:-/etc/athena/secrets.env}\n      required: false" in text
    # no token interpolated into the compose configuration (the server checks it itself)
    assert "${CONVO_MCP_TOKEN" not in text
    assert text.count('security_opt: ["no-new-privileges:true"]') == 2  # x-sound + whisper
    assert text.count("cap_drop: [ALL]") == 2
    # whisper runs as the data dir's user, and its models follow the data dir
    whisper = text[text.index("  whisper-gpu:"):]
    assert 'user: "${PUID:-1000}:${PGID:-1000}"' in whisper
    assert "${CONVO_DOCKER_WHISPER_MODELS:-${CONVO_DATA_DIR:-./data}/whisper-models}" in whisper


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker compose CLI")
def test_compose_files_resolve(tmp_path):
    for name in ("compose.yml", "compose.gpu.yml", "compose.network.yml"):
        shutil.copy(SERVER / name, tmp_path / name)
    (tmp_path / ".env").write_text(f"CONVO_INGEST_TOKEN={INGEST}\nCOMPOSE_PROFILES=whisper-gpu\n"
                                   "CONVO_DATA_DIR=/srv/athena/data\n")
    secret = secrets(tmp_path, f"CONVO_MCP_TOKEN={MCP}\n")
    env = {**os.environ, "ATHENA_SECRETS_FILE": str(secret), "ATHENA_NET_SUBNET": "172.29.10.0/24"}
    out = subprocess.run(["docker", "compose", "-f", "compose.yml", "-f", "compose.gpu.yml",
                          "-f", "compose.network.yml", "config", "--format", "json"],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    if out.returncode != 0 and "docker: 'compose' is not a docker command" in out.stderr:
        pytest.skip("no compose plugin")
    assert out.returncode == 0, out.stderr
    import json
    cfg = json.loads(out.stdout)
    mcp = cfg["services"]["sound-mcp"]["environment"]
    assert mcp["CONVO_MCP_TOKEN"] == MCP  # from the secrets file
    whisper = cfg["services"]["whisper-gpu"]
    assert whisper["volumes"][0]["source"] == "/srv/athena/data/whisper-models"
    assert whisper["user"] == "1000:1000"
    net = cfg["networks"]["default"]
    assert net["name"] == "athena-net" and net["ipam"]["config"][0]["subnet"] == "172.29.10.0/24"
    assert net["driver_opts"]["com.docker.network.bridge.name"] == "br-athena"
