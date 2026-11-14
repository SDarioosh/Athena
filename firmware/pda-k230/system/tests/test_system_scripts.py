"""Shell-level tests of the image's root-run scripts (firmware/pda-k230/system).

The scripts source `/usr/libexec/athena-pda/athena-pda-common.sh` and call
`/etc/init.d/*` by absolute path; each test copies a script into a temp directory with
those paths pointing at stand-ins there, and runs it with `/bin/sh`.

As root (the sandbox) the PDA user is `nobody`, so the privilege drop is exercised for
real; as another user (CI) the PDA user is that user.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

FILES = Path(__file__).resolve().parents[1] / "br2-external" / "package"
SYSTEM = FILES / "athena-pda-system" / "files"
COMMON = SYSTEM / "athena-pda-common.sh"
REAL_COMMON = "/usr/libexec/athena-pda/athena-pda-common.sh"

IS_ROOT = os.getuid() == 0
PDA_USER = "nobody" if IS_ROOT else pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def box() -> Iterator[Path]:
    """A temp directory the PDA user can enter (as root, pytest's tmp_path is 0700)."""
    d = Path(tempfile.mkdtemp(prefix="athena-sys-"))
    d.chmod(0o711)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def install(box: Path, script: Path, *, defaults: str = "") -> Path:
    """`script` copied into `box/bin`, sourcing a common.sh whose defaults point into box."""
    (box / "bin").mkdir(exist_ok=True)
    (box / "initd").mkdir(exist_ok=True)
    for name in ("S45tailscaled", "S93athena-pda-syncd"):
        stub = box / "initd" / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"{box}/initd.log"\n')
        stub.chmod(0o755)
    data = box / "data"
    data.mkdir(exist_ok=True)
    if IS_ROOT:
        pw = pwd.getpwnam(PDA_USER)
        os.chown(data, pw.pw_uid, pw.pw_gid)
    data.chmod(0o700)
    (box / "default").write_text(
        f"ATHENA_PDA_DIR={data}\nATHENA_PDA_USER={PDA_USER}\nATHENA_PDA_RUN={box}/run\n"
        + defaults)
    common = box / "bin" / "common.sh"
    common.write_text(
        COMMON.read_text()
        # athena_dirs as root would mkdir/chown under /run: the test makes the dirs
        + "\nathena_dirs() { :; }\n")
    text = script.read_text().replace(REAL_COMMON, str(common))
    text = text.replace("/etc/init.d/", f"{box}/initd/")
    out = box / "bin" / script.name
    out.write_text(text)
    out.chmod(0o755)
    return out


def run(box: Path, script: Path, *args: str,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    e = {"PATH": os.environ["PATH"], "ATHENA_PDA_DEFAULTS": str(box / "default")}
    e.update(env or {})
    return subprocess.run(["/bin/sh", str(script), *args], capture_output=True, text=True,
                          env=e, timeout=30, check=False)


# ---- athena-tailnet mode: config.json in the PDA user's directory (finding 1) ----


def test_mode_userspace_sets_proxy_and_keeps_other_keys(box: Path) -> None:
    script = install(box, SYSTEM / "athena-tailnet")
    cfg = box / "data" / "config.json"
    cfg.write_text(json.dumps({"tz": "Europe/Berlin"}))
    if IS_ROOT:
        pw = pwd.getpwnam(PDA_USER)
        os.chown(cfg, pw.pw_uid, pw.pw_gid)
    r = run(box, script, "mode", "userspace")
    assert r.returncode == 0, r.stderr
    assert json.loads(cfg.read_text()) == {"tz": "Europe/Berlin",
                                           "proxy": "http://127.0.0.1:1055"}
    st = cfg.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert st.st_uid == pwd.getpwnam(PDA_USER).pw_uid
    assert "ATHENA_TS_MODE=userspace" in (box / "default").read_text()
    assert (box / "initd.log").read_text().split("\n")[:2] == [
        "S45tailscaled restart", "S93athena-pda-syncd restart"]
    r = run(box, script, "mode", "tun")
    assert r.returncode == 0, r.stderr
    assert json.loads(cfg.read_text()) == {"tz": "Europe/Berlin"}


def test_mode_does_not_follow_a_planted_temp_symlink(box: Path) -> None:
    """The PDA user plants config.json.tmp -> a file it can't write: root must neither
    truncate nor chown that file."""
    script = install(box, SYSTEM / "athena-tailnet")
    victim_dir = box / "root-only"
    victim_dir.mkdir()
    victim_dir.chmod(0o755)
    victim = victim_dir / "shadow"
    victim.write_text("root:secret\n")
    victim.chmod(0o644)
    before = victim.stat()
    (box / "data" / "config.json.tmp").symlink_to(victim)
    r = run(box, script, "mode", "userspace")
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "root:secret\n"
    after = victim.stat()
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    cfg = box / "data" / "config.json"
    assert not cfg.is_symlink()
    assert json.loads(cfg.read_text()) == {"proxy": "http://127.0.0.1:1055"}


def test_mode_does_not_read_through_a_config_symlink(box: Path) -> None:
    """config.json -> a root-only file: its content must not end up in config.json."""
    script = install(box, SYSTEM / "athena-tailnet")
    secret = box / "secret.json"
    secret.write_text(json.dumps({"secret": "x"}))
    secret.chmod(0o600 if IS_ROOT else 0o644)
    (box / "data" / "config.json").symlink_to(secret)
    r = run(box, script, "mode", "userspace")
    assert r.returncode != 0
    assert "symlink" in r.stderr
    assert json.loads(secret.read_text()) == {"secret": "x"}
    assert (box / "data" / "config.json").is_symlink()


# ---- athena-tailnet watch: root starts the login, the PDA user is no operator (finding 2) ----

FAKE_TAILSCALE = """#!/bin/sh
echo "$*" >>"{box}/ts.log"
for a in "$@"; do
	case "$a" in
		status) cat "{box}/status.json"; exit 0 ;;
		login) echo $$ >"{box}/login.pid"; exec sleep 30 ;;
		set) exit 0 ;;
	esac
done
"""

FAST = ("ATHENA_TS_WATCH_LOGIN_POLL=0.2\nATHENA_TS_WATCH_POLL=0.2\n"
        "ATHENA_TS_LOGIN_RETRY=0\nATHENA_TS_LOGIN_TIMEOUT=7\n")


def _watch(box: Path, backend: str, seconds: float = 1.5) -> list[str]:
    fake = box / "bin" / "fake-tailscale"
    script = install(box, SYSTEM / "athena-tailnet", defaults=FAST)
    script.write_text(script.read_text().replace("/usr/bin/tailscale", str(fake)))
    fake.write_text(FAKE_TAILSCALE.format(box=box))
    fake.chmod(0o755)
    (box / "status.json").write_text(json.dumps({"BackendState": backend, "AuthURL": ""}))
    e = {"PATH": os.environ["PATH"], "ATHENA_PDA_DEFAULTS": str(box / "default")}
    p = subprocess.Popen(["/bin/sh", str(script), "watch"], env=e,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(seconds)
    finally:
        p.terminate()
        p.wait(timeout=10)
    assert p.returncode == 0, p.stderr.read() if p.stderr else ""
    return (box / "ts.log").read_text().splitlines()


def _sock(line: str) -> str:
    return line.split(" ", 1)[1] if line.startswith("--socket=") else line


def test_watch_sets_no_operator_then_logs_in_once(box: Path) -> None:
    calls = [_sock(c) for c in _watch(box, "NeedsLogin")]
    flags = "--operator= --hostname=athena-pda --netfilter-mode=off"
    assert calls[0] == f"set {flags}"
    logins = [c for c in calls if c.startswith("login")]
    # one login while the first is still running (no retry storm), with the fixed flags
    assert logins == [f"login {flags} --timeout=7s"]
    assert any(c == "status --json" for c in calls)
    assert not any(f"--operator={PDA_USER}" in c for c in calls)
    # TERM ended the watch and also the login it started
    pid = int((box / "login.pid").read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


@pytest.mark.parametrize("backend", ["Running", "NeedsMachineAuth", "Stopped", "Starting"])
def test_watch_starts_no_login_unless_needed(box: Path, backend: str) -> None:
    calls = [_sock(c) for c in _watch(box, backend, 1.0)]
    assert calls[0].startswith("set ")
    assert "status --json" in calls
    assert not [c for c in calls if c.startswith("login")]


def test_no_user_reaches_tailscale_flags() -> None:
    """The PATH wrapper that let syncd run `tailscale login <flags>` is gone, and the boot
    scripts give no user the operator."""
    assert not (SYSTEM / "tailscale-wrapper").exists()
    for f in [*SYSTEM.iterdir(), *(FILES / "athena-tailscale-bin").iterdir(),
              *(FILES / "athena-pda-syncd").iterdir()]:
        if f.is_file():
            assert "--operator=$" not in f.read_text(), f
            assert "libexec/athena-pda/bin" not in f.read_text(), f
