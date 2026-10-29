"""server/scripts/deploy.sh with Docker: the exact compose calls of start and deploy.

Every tool the script runs is a fake on PATH (docker, systemctl, curl, loginctl) that
records its arguments, and ``deploy`` runs in a throwaway git checkout with a bare
"remote", so nothing here touches a real service, container or the repo itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy.sh"
SERVER = SCRIPT.parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")

INGEST = "fictional-ingest-token-0123456789"
NONE_ACTIVE = '{"meetings": [{"id": "m0", "state": "ended", "active": false}]}'
ACTIVE = ('{"meetings": [{"id": "m1", "state": "recording", "active": true, '
          '"meeting_id": "81234567890", "title": "Budget sync (fictional)"}]}')

FAKE_DOCKER = r'''#!/usr/bin/env bash
# fake docker: logs its arguments; behaviour from files in $FAKE
echo "docker $*" >> "$FAKE/log"
args=" $* "
case $args in
  *" compose version "*) [ -f "$FAKE/no-compose" ] && exit 1; echo "Docker Compose version v2.29.0"; exit 0 ;;
  *" --profile * config --services "*) printf 'sound-api\nsound-mcp\nwhisper-gpu\nwhisper-cpu\n'; exit 0 ;;
  *" config --services "*) printf 'sound-api\nsound-mcp\nwhisper-cpu\n'; exit 0 ;;
  *" compose "*" build"*) [ -f "$FAKE/build-fails" ] && exit 1; exit 0 ;;
  *" compose "*" up "*) [ -f "$FAKE/up-fails" ] && exit 1; exit 0 ;;
  *" down "*)
    p=$(printf '%s\n' "$@" | grep -A1 -x -- -p | tail -n 1)
    rm -f "$FAKE/project-$p"; exit 0 ;;
  *" ps -aq --filter label=com.docker.compose.project="*)
    p=${args##*com.docker.compose.project=}; p=${p%% *}
    [ -f "$FAKE/project-$p" ] && cat "$FAKE/project-$p"; exit 0 ;;
esac
exit 0
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
# fake systemctl: logs its arguments; a unit listed in $FAKE/enabled is enabled and active
echo "systemctl $*" >> "$FAKE/log"
case " $* " in
  *" is-enabled "*|*" is-active "*)
    for a in "$@"; do :; done
    grep -qx -- "$a" "$FAKE/enabled" 2>/dev/null; exit ;;
  *" show-environment "*) exit 0 ;;
esac
exit 0
'''


class Env:
    """The fakes of one test: ``env`` to run the script with, ``log()`` what they did."""

    def __init__(self, tmp: Path, meetings: str | None = NONE_ACTIVE):
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.fake.mkdir()
        self.bin.mkdir()
        (self.fake / "body.json").write_text(meetings or "")
        tools = {
            "docker": FAKE_DOCKER,
            "systemctl": FAKE_SYSTEMCTL,
            "loginctl": "#!/usr/bin/env bash\necho yes\n",
            "journalctl": "#!/usr/bin/env bash\nexit 0\n",
            "curl": textwrap.dedent(f"""\
                #!/usr/bin/env bash
                case "${{@: -1}}" in
                  */v1/meetings*)
                    echo "curl meetings" >> "$FAKE/log"; cat > /dev/null
                    {"exit 7" if meetings is None else ""}
                    cat "$FAKE/body.json"; printf '\\n200' ;;
                  *) exit 0 ;;
                esac
                """),
        }
        for name, body in tools.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.cfg = tmp / "cfg"
        self.data = tmp / "data"
        self.data.mkdir()
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("CONVO_", "BARVIS_SOUND_", "ATHENA_", "FORK_", "DOCKER",
                                     "COMPOSE_"))}
        self.env = {**base, "PATH": f"{self.bin}:{base['PATH']}", "FAKE": str(self.fake),
                    "HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(self.cfg),
                    "CONVO_DATA_DIR": str(self.data), "CONVO_INGEST_TOKEN": INGEST,
                    "CONVO_MCP_TOKEN": "fictional-mcp-token-0123456789",
                    "CONVO_PORT": "18765", "DOCKER": "docker",
                    "BARVIS_SOUND_ODYSSEUS_DIR": "off"}

    def log(self) -> list[str]:
        f = self.fake / "log"
        return f.read_text().splitlines() if f.exists() else []

    def clear(self) -> None:
        (self.fake / "log").unlink(missing_ok=True)

    def native_unit(self, name: str, enabled: bool = True) -> Path:
        """An older native deploy's unit file (and, if enabled, its unit is running)."""
        unit_dir = self.cfg / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / f"{name}.service"
        unit.write_text("# Written by odysseus-sound server/scripts/deploy.sh\n")
        if enabled:
            with (self.fake / "enabled").open("a") as f:
                f.write(name + "\n")
        return unit

    def old_project(self, project: str = "odysseus-sound") -> None:
        """Containers of compose project ``project`` exist."""
        (self.fake / f"project-{project}").write_text("c0ffee000001\nc0ffee000002\n")


def run_script(script: Path, *args: str, env: dict, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env=env, timeout=60, stdin=stdin)


def compose_calls(e: Env, server: Path = SERVER) -> list[str]:
    """The docker calls, with the compose prefix of this checkout shortened to ``DC``."""
    prefix = f"docker compose --project-directory {server} -f {server}/compose.yml"
    return [line.replace(prefix, "DC") for line in e.log() if line.startswith("docker ")]


GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def checkout(tmp: Path) -> tuple[Path, Path]:
    """A clone of a bare "SDarioosh/Athena" remote holding this deploy.sh and compose
    files; the remote is one commit ahead. Returns (repo dir, its server/scripts/deploy.sh)."""
    remote = tmp / "remote" / "SDarioosh" / "Athena.git"
    seed = tmp / "seed"
    (seed / "server" / "scripts").mkdir(parents=True)
    for name in ("compose.yml", "compose.gpu.yml", "compose.meeting.yml", ".env.example"):
        shutil.copy(SERVER / name, seed / "server" / name)
    shutil.copy(SCRIPT, seed / "server" / "scripts" / "deploy.sh")
    (seed / ".gitignore").write_text("server/.env\n")
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    subprocess.run([*GIT, "-C", str(seed), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "first"], check=True)
    subprocess.run([*GIT, "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    repo = tmp / "athena"
    subprocess.run([*GIT, "clone", "-q", str(remote), str(repo)], check=True)
    (seed / "NEWS").write_text("second\n")
    subprocess.run([*GIT, "-C", str(seed), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "second"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "main"], check=True)
    return repo, repo / "server" / "scripts" / "deploy.sh"


def head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


# --- characterization: the docker runner's compose calls -------------------------------

def test_docker_start_compose_calls(tmp_path):
    e = Env(tmp_path)
    r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_RUNNER": "docker",
                                         "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    assert compose_calls(e) == [
        "DC config --services",
        "DC --profile * config --services",
        "DC rm -sf whisper-gpu",
        "DC up -d --build --remove-orphans --wait --wait-timeout 300",
    ]


def test_docker_deploy_compose_calls_and_checkout(tmp_path):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    before = head(repo)
    r = run_script(script, "deploy", env={**e.env, "BARVIS_SOUND_RUNNER": "docker",
                                          "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    assert head(repo) != before
    assert (repo / "server" / ".env").exists()  # first_env on a new machine
    server = repo / "server"
    assert compose_calls(e, server) == [
        "DC config --services",
        "DC --profile * config --services",
        "DC rm -sf whisper-gpu",
        "DC up -d --build --remove-orphans --wait --wait-timeout 300",
    ]
    assert "deployed" in r.stdout and "(docker)" in r.stdout


def test_docker_deploy_retires_an_enabled_native_unit_and_keeps_its_file(tmp_path):
    e = Env(tmp_path)
    unit = e.native_unit("barvis-sound")
    e.native_unit("barvis-sound-mcp")
    r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_RUNNER": "docker",
                                         "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    log = e.log()
    assert "systemctl --user disable -q --now barvis-sound" in log
    assert "systemctl --user disable -q --now barvis-sound-mcp" in log
    assert unit.exists()  # the way back
    assert "stopping native barvis-sound (it runs in Docker now)" in r.stdout


def test_hub_call_shape(tmp_path):
    """The hub's scripts/fork_deploy.sh runs each FORK_MODULES entry as
    ``FORK_DEPLOY_RUNNING=1 <deploy.sh> deploy < /dev/null`` and ``<deploy.sh> status <
    /dev/null``; Athena must not deploy Odysseus itself then."""
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    hub = tmp_path / "hub"
    (hub / "scripts").mkdir(parents=True)
    hub_calls = tmp_path / "hub-calls"
    (hub / "scripts" / "fork_deploy.sh").write_text(
        f"#!/usr/bin/env bash\necho \"$*\" >> {hub_calls}\n")
    (hub / "scripts" / "fork_deploy.sh").chmod(0o755)
    env = {**e.env, "BARVIS_SOUND_RUNNER": "docker", "BARVIS_SOUND_ZOOM_SDK_DIR": "off",
           "BARVIS_SOUND_ODYSSEUS_DIR": str(hub)}
    with open(os.devnull) as devnull:
        r = subprocess.run(["bash", str(script), "deploy"], capture_output=True, text=True,
                           timeout=60, stdin=devnull, env={**env, "FORK_DEPLOY_RUNNING": "1"})
        assert r.returncode == 0, r.stderr
        assert not hub_calls.exists()
        r = subprocess.run(["bash", str(script), "status"], capture_output=True, text=True,
                           timeout=60, stdin=devnull, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Athena: ")
    assert any(" ps --format" in line for line in e.log())
