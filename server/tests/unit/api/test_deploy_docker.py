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
                          env=env, timeout=180, stdin=stdin)


def compose_calls(e: Env, server: Path = SERVER) -> list[str]:
    """The docker calls, with the compose prefix of this checkout shortened to ``DC``."""
    prefix = f"docker compose --project-directory {server} -f {server}/compose.yml"
    return [line.replace(prefix, "DC") for line in e.log() if line.startswith("docker ")]


GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def checkout(tmp: Path, project: str | None = None) -> tuple[Path, Path]:
    """A clone of a bare "SDarioosh/Athena" remote holding this deploy.sh and compose
    files; the remote is one commit ahead. Returns (repo dir, its server/scripts/deploy.sh).
    ``project``: the compose project name compose.yml has (a rename)."""
    remote = tmp / "remote" / "SDarioosh" / "Athena.git"
    seed = tmp / "seed"
    (seed / "server" / "scripts").mkdir(parents=True)
    for name in ("compose.yml", "compose.gpu.yml", "compose.meeting.yml", ".env.example"):
        shutil.copy(SERVER / name, seed / "server" / name)
    shutil.copy(SCRIPT, seed / "server" / "scripts" / "deploy.sh")
    if project:
        compose = seed / "server" / "compose.yml"
        compose.write_text(compose.read_text().replace("\nname: odysseus-sound\n",
                                                       f"\nname: {project}\n"))
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


# --- the docker runner's compose calls ----------------------------------------------------
# Pinned on the deploy.sh before it became Docker only; since then two read-only checks come
# first: that compose works, and whether containers exist under Athena's other compose
# project name (compose.yml still says odysseus-sound; the other name is athena).

CHECKS = [
    "docker compose version",
    "docker ps -aq --filter label=com.docker.compose.project=athena",
]


def test_docker_start_compose_calls(tmp_path):
    e = Env(tmp_path)
    r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_RUNNER": "docker",
                                         "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    assert compose_calls(e) == [
        *CHECKS,
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
        *CHECKS,
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
                                         "BARVIS_SOUND_ZOOM_SDK_DIR": "off",
                                         "COMPOSE_PROFILES": "whisper-gpu"})
    assert r.returncode == 0, r.stderr
    log = e.log()
    assert "systemctl --user disable -q --now barvis-sound" in log
    assert "systemctl --user disable -q --now barvis-sound-mcp" in log
    assert unit.exists()  # the way back
    assert "stopping native barvis-sound (it runs in Docker now; its unit file stays)" in r.stdout
    # the images are built before anything native is stopped
    build = log.index(f"docker compose --project-directory {SERVER} -f {SERVER}/compose.yml build")
    assert build < log.index("systemctl --user disable -q --now barvis-sound")
    assert log.index("systemctl --user disable -q --now barvis-sound-mcp") < next(
        i for i, line in enumerate(log) if " up -d --build" in line)


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
                           timeout=180, stdin=devnull, env={**env, "FORK_DEPLOY_RUNNING": "1"})
        assert r.returncode == 0, r.stderr
        assert not hub_calls.exists()
        r = subprocess.run(["bash", str(script), "status"], capture_output=True, text=True,
                           timeout=180, stdin=devnull, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Athena: ")
    assert any(" ps --format" in line for line in e.log())


# --- a native install is never switched silently ----------------------------------------

def changes(e: Env) -> list[str]:
    """Calls that stop, build, start or remove anything (fakes' log)."""
    verbs = (" disable", " stop", " restart", " build", " up ", " down", " rm ", " kill")
    return [line for line in e.log() if any(v in f"{line} " for v in verbs)]


@pytest.mark.parametrize("runner", [None, "auto"])
def test_native_units_without_explicit_docker_refuse_and_change_nothing(tmp_path, runner):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    before = head(repo)
    unit = e.native_unit("barvis-sound")
    e.native_unit("barvis-sound-mcp")
    e.native_unit("barvis-sound-whisper", enabled=False)  # its file only: not running
    env = {**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"}
    if runner:
        env["ATHENA_RUNNER"] = runner
    for cmd in ("deploy", "start", "restart", "stop"):
        r = run_script(script, cmd, env=env)
        assert r.returncode == 1, (cmd, r.stdout)
        assert f"not running '{cmd}': Athena runs natively on this machine" in r.stderr
        assert "barvis-sound (systemd user service" in r.stderr
        assert "barvis-sound-mcp (systemd user service" in r.stderr
        assert "barvis-sound-whisper" not in r.stderr
        assert "Nothing was changed." in r.stderr
        assert "ATHENA_RUNNER=docker" in r.stderr and "COMPOSE_PROFILES=whisper-gpu" in r.stderr
        assert "CONVO_DOCKER_WHISPER_URL" in r.stderr and "docker compose -p odysseus-sound down" in r.stderr
        assert changes(e) == [] and "curl meetings" not in e.log()  # refused first
    assert head(repo) == before and not (repo / "server" / ".env").exists()
    assert unit.exists()


def test_native_background_process_refuses_and_keeps_running(tmp_path):
    e = Env(tmp_path)
    # not our child (as a background deploy's process isn't), so it is reaped once killed
    pid = int(subprocess.run(["bash", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                             capture_output=True, text=True, check=True).stdout)

    def alive() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"

    try:
        (e.data / "run").mkdir()
        (e.data / "run" / "serve.pid").write_text(f"{pid}\n")
        r = run_script(SCRIPT, "start", env=e.env)
        assert r.returncode == 1
        assert f"barvis-sound (background process {pid}" in r.stderr
        assert changes(e) == [] and alive()
        # with the runner set to docker (and a whisper choice) it is stopped, after the build
        r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_RUNNER": "docker",
                                             "CONVO_DOCKER_WHISPER_URL": "http://127.0.0.1:8178"})
        assert r.returncode == 0, r.stderr
        assert "stopping background barvis-sound (it runs in Docker now)" in r.stdout
        assert not alive()
        assert not (e.data / "run" / "serve.pid").exists()
    finally:
        if alive():
            os.kill(pid, 9)


@pytest.mark.parametrize("name", ["ATHENA_RUNNER", "BARVIS_SOUND_RUNNER"])
@pytest.mark.parametrize("value", ["systemd", "background"])
def test_old_runner_setting_is_a_clear_error_before_anything(tmp_path, name, value):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    before = head(repo)
    r = run_script(script, "deploy", env={**e.env, name: value})
    assert r.returncode == 1
    assert f"the runner is set to {value}" in r.stderr
    assert "now runs Athena only in Docker" in r.stderr and "ATHENA_RUNNER=docker" in r.stderr
    assert e.log() == [] and head(repo) == before


def test_unknown_runner(tmp_path):
    e = Env(tmp_path)
    r = run_script(SCRIPT, "start", env={**e.env, "ATHENA_RUNNER": "podman"})
    assert r.returncode == 1 and "must be docker or auto" in r.stderr and e.log() == []


def test_explicit_docker_on_a_native_install_needs_a_whisper_choice(tmp_path):
    e = Env(tmp_path)
    e.native_unit("barvis-sound")
    r = run_script(SCRIPT, "start", env={**e.env, "ATHENA_RUNNER": "docker"})
    assert r.returncode == 1 and "no whisper is chosen for Docker" in r.stderr
    assert changes(e) == []
    for whisper in ({"COMPOSE_PROFILES": "whisper-cpu"},
                    {"CONVO_DOCKER_WHISPER_URL": "http://127.0.0.1:8178"}):
        e.clear()
        r = run_script(SCRIPT, "start", env={**e.env, "ATHENA_RUNNER": "docker", **whisper})
        assert r.returncode == 0, r.stderr
        assert "systemctl --user disable -q --now barvis-sound" in e.log()
        (e.fake / "enabled").write_text("barvis-sound\n")  # running again for the next round


def test_explicit_docker_migration_through_deploy(tmp_path):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    e.native_unit("barvis-sound")
    r = run_script(script, "deploy", env={**e.env, "ATHENA_RUNNER": "docker",
                                          "COMPOSE_PROFILES": "whisper-gpu"})
    assert r.returncode == 0, r.stderr
    server = repo / "server"
    assert compose_calls(e, server) == [
        "docker compose version",
        "DC build",
        "docker ps -aq --filter label=com.docker.compose.project=athena",
        "DC config --services",
        "DC --profile * config --services",
        "DC rm -sf whisper-gpu",
        "DC up -d --build --remove-orphans --wait --wait-timeout 300",
    ]
    assert "systemctl --user disable -q --now barvis-sound" in e.log()


def test_build_failure_while_switching_stops_nothing(tmp_path):
    e = Env(tmp_path)
    e.native_unit("barvis-sound")
    e.old_project()
    (e.fake / "build-fails").touch()
    r = run_script(SCRIPT, "start", env={**e.env, "ATHENA_RUNNER": "docker",
                                         "COMPOSE_PROFILES": "whisper-gpu"})
    assert r.returncode == 1 and "build failed; nothing was stopped" in r.stdout
    assert not any(" disable" in c or " down" in c or " up " in c for c in e.log())


def test_docker_missing_fails_before_anything(tmp_path):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    before = head(repo)
    e.native_unit("barvis-sound")
    (e.fake / "no-compose").touch()
    r = run_script(script, "deploy", env={**e.env, "ATHENA_RUNNER": "docker",
                                          "COMPOSE_PROFILES": "whisper-gpu"})
    assert r.returncode == 1 and "'docker compose version' failed" in r.stderr
    assert "Nothing was changed" in r.stderr
    assert changes(e) == [] and head(repo) == before


def test_no_native_install_auto_is_docker(tmp_path):
    e = Env(tmp_path)
    e.native_unit("barvis-sound", enabled=False)  # left by an earlier switch: not running
    r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    assert any(" up -d --build" in c for c in e.log())
    assert not any(" disable" in c for c in e.log())


# --- compose project names odysseus-sound and athena -----------------------------------

def test_compose_project_keeps_its_name_for_now():
    # the first deploy after a change runs the deploy.sh from before it, which would start
    # an "athena" project next to the running odysseus-sound one (same ports) and roll back
    text = (SERVER / "compose.yml").read_text()
    assert "\nname: odysseus-sound\n" in text


def renamed(tmp: Path) -> tuple[Path, Path]:
    """A checkout whose compose.yml says name: athena (the rename, done later)."""
    repo, script = checkout(tmp, project="athena")
    return repo / "server", script


def test_old_project_is_taken_down_after_the_build_right_before_the_new_one(tmp_path):
    e = Env(tmp_path)
    server, script = renamed(tmp_path)
    e.old_project("odysseus-sound")
    env = {**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"}
    r = run_script(script, "start", env=env)
    assert r.returncode == 0, r.stderr
    assert compose_calls(e, server) == [
        "docker compose version",
        "docker ps -aq --filter label=com.docker.compose.project=odysseus-sound",
        "DC build",
        "docker ps -aq --filter label=com.docker.compose.project=odysseus-sound",
        "DC -p odysseus-sound --profile * down --remove-orphans",
        "DC config --services",
        "DC --profile * config --services",
        "DC rm -sf whisper-gpu",
        "DC up -d --build --remove-orphans --wait --wait-timeout 300",
    ]
    assert "taking down compose project odysseus-sound" in r.stdout
    # idempotent: the second run finds no old containers and takes nothing down
    e.clear()
    r = run_script(script, "start", env=env)
    assert r.returncode == 0, r.stderr
    assert not any(" down" in c or c.endswith(" build") for c in e.log())


def test_rollback_direction_takes_the_athena_project_down(tmp_path):
    """compose.yml says odysseus-sound (a rollback to before the rename): athena goes."""
    e = Env(tmp_path)
    e.old_project("athena")
    r = run_script(SCRIPT, "start", env={**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    calls = compose_calls(e)
    assert calls.index("DC build") < calls.index("DC -p athena --profile * down --remove-orphans") \
        < calls.index("DC up -d --build --remove-orphans --wait --wait-timeout 300")


def test_old_project_never_taken_down_during_a_meeting(tmp_path):
    e = Env(tmp_path, meetings=ACTIVE)
    _, script = renamed(tmp_path)
    e.old_project("odysseus-sound")
    for cmd in ("start", "restart", "deploy"):
        r = run_script(script, cmd, env={**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
        assert r.returncode == 1 and "Athena is in a meeting right now" in r.stderr
        assert changes(e) == []


def test_old_project_with_a_failed_build_keeps_running(tmp_path):
    e = Env(tmp_path)
    _, script = renamed(tmp_path)
    e.old_project("odysseus-sound")
    (e.fake / "build-fails").touch()
    r = run_script(script, "start", env={**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 1
    assert not any(" down" in c or " up " in c for c in e.log())
    assert (e.fake / "project-odysseus-sound").exists()


def test_restart_with_an_old_project_switches_over(tmp_path):
    e = Env(tmp_path)
    server, script = renamed(tmp_path)
    e.old_project("odysseus-sound")
    r = run_script(script, "restart", env={**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"})
    assert r.returncode == 0, r.stderr
    calls = compose_calls(e, server)
    assert calls.index("DC -p odysseus-sound --profile * down --remove-orphans") < calls.index(
        "DC up -d --build --remove-orphans --wait --wait-timeout 300")


def test_status_names_a_native_install_and_an_old_project_and_changes_nothing(tmp_path):
    e = Env(tmp_path)
    _, script = renamed(tmp_path)
    e.native_unit("barvis-sound")
    e.old_project("odysseus-sound")
    with open(os.devnull) as devnull:
        r = run_script(script, "status", env=e.env, stdin=devnull)
    assert r.returncode == 0, r.stderr
    assert "(docker, project athena)" in r.stdout
    assert "still running natively" in r.stdout and "barvis-sound (systemd user service" in r.stdout
    assert "containers of the former compose project odysseus-sound" in r.stdout
    assert changes(e) == []


# --- ATHENA_* settings, old BARVIS_SOUND_* names still read -----------------------------

def odysseus_dir_used(e: Env, script: Path = SCRIPT, **env: str) -> str:
    r = run_script(script, "odysseus", env={**{k: v for k, v in e.env.items()
                                               if k != "BARVIS_SOUND_ODYSSEUS_DIR"}, **env})
    assert r.returncode == 1, r.stdout
    return r.stderr.split("no scripts/fork_deploy.sh in ")[1].split(" ")[0]


def test_athena_name_wins_old_name_still_works(tmp_path):
    e = Env(tmp_path)
    assert odysseus_dir_used(e, BARVIS_SOUND_ODYSSEUS_DIR="/old") == "/old"
    assert odysseus_dir_used(e, ATHENA_ODYSSEUS_DIR="/new") == "/new"
    assert odysseus_dir_used(e, ATHENA_ODYSSEUS_DIR="/new", BARVIS_SOUND_ODYSSEUS_DIR="/old") == "/new"


def test_dotenv_athena_name_wins_and_environment_beats_dotenv(tmp_path):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    dotenv = repo / "server" / ".env"
    dotenv.write_text("BARVIS_SOUND_ODYSSEUS_DIR=/old\n")
    assert odysseus_dir_used(e, script) == "/old"
    dotenv.write_text("ATHENA_ODYSSEUS_DIR=/new\nBARVIS_SOUND_ODYSSEUS_DIR=/old\n")
    assert odysseus_dir_used(e, script) == "/new"
    # the environment wins over .env, also under the old name
    assert odysseus_dir_used(e, script, BARVIS_SOUND_ODYSSEUS_DIR="/env-old") == "/env-old"


@pytest.mark.parametrize("names", [("ATHENA_REMOTE",), ("BARVIS_SOUND_REMOTE",),
                                   ("ATHENA_REMOTE", "BARVIS_SOUND_REMOTE")])
def test_remote_and_branch_settings(tmp_path, names):
    e = Env(tmp_path)
    repo, script = checkout(tmp_path)
    env = {**e.env, "BARVIS_SOUND_ZOOM_SDK_DIR": "off"}
    for n in names:
        env[n] = "no-such-remote" if n.startswith("BARVIS") and len(names) == 2 else "origin"
    r = run_script(script, "deploy", env=env)
    assert r.returncode == 0, r.stderr
    assert "fetching origin" in r.stdout
    r = run_script(script, "deploy", env={**env, "ATHENA_BRANCH": "nope",
                                          "BARVIS_SOUND_BRANCH": "main"})
    assert r.returncode != 0 and "origin/nope" in r.stderr  # the ATHENA_ name was used


@pytest.mark.parametrize("name", ["ATHENA_ZOOM_SDK_DIR", "BARVIS_SOUND_ZOOM_SDK_DIR"])
def test_zoom_sdk_dir_under_either_name(tmp_path, name):
    e = Env(tmp_path)
    sdk = tmp_path / "sdk"
    (sdk / "h").mkdir(parents=True)
    (sdk / "h" / "zoom_sdk.h").write_text("/* fictional */\n")
    r = run_script(SCRIPT, "start", env={**e.env, name: str(sdk), "CONVO_ZOOM_SDK_KEY": "k",
                                         "CONVO_ZOOM_SDK_SECRET": "s"})
    assert r.returncode == 0, r.stderr
    assert any(f"-f {SERVER}/compose.meeting.yml up -d" in c for c in e.log())
    # ATHENA_ZOOM_SDK_DIR=off wins over the old name
    e.clear()
    run_script(SCRIPT, "start", env={**e.env, "ATHENA_ZOOM_SDK_DIR": "off",
                                     "BARVIS_SOUND_ZOOM_SDK_DIR": str(sdk)})
    assert not any("compose.meeting.yml" in c for c in e.log())


def test_hub_status_on_a_new_machine_does_not_fail(tmp_path):
    e = Env(tmp_path)
    with open(os.devnull) as devnull:
        r = run_script(SCRIPT, "status", env=e.env, stdin=devnull)
    assert r.returncode == 0 and "still running natively" not in r.stdout
