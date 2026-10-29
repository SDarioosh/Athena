"""A long-running child process, controlled from the server's event loop.

Generic process control, with no knowledge of what the child is (the meeting bot's
launcher, ``launcher.py``, is the user): its command under safety caps (a
``systemd-run --user --scope`` with ``CPUQuota``, ``MemoryMax`` and ``RuntimeMaxSec``, or
``nice 10`` and ``RLIMIT_AS``), a start in its own session and process group with an
environment built from scratch, its exit awaited on the loop (a pidfd, no thread), its
stdout read line by line, and a stop: SIGTERM, then SIGKILL to its process group after a
grace. Nothing here logs; callers say what happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

MAX_LINE = 4096  # a longer stdout line is dropped whole, never parsed
# Environment a child keeps from the server's (nothing else: no server secrets).
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


# ---------------------------------------------------------------- command, start
def child_env(extra: dict[str, str]) -> dict[str, str]:
    """The child's environment: ``KEEP_ENV`` from the server's, plus the non-empty
    values of ``extra``."""
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env.update({k: v for k, v in extra.items() if v})
    return env


def scope_command(argv: list[str], *, unit: str, cpu: str, memory: str,
                  runtime_max: int) -> list[str]:
    """``argv`` in a transient systemd user scope with its caps, at nice 10."""
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={unit}", "-p", f"CPUQuota={cpu}", "-p", f"MemoryMax={memory}",
            "-p", f"RuntimeMaxSec={runtime_max}", "nice", "-n", "10", *argv]


def scope_probe_command() -> list[str]:
    """A command that succeeds only where user scopes can be started."""
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"]


def scope_stop_command(unit: str) -> list[str]:
    return ["systemctl", "--user", "stop", f"{unit}.scope"]


def nice_command(argv: list[str]) -> list[str]:
    """``argv`` at nice 10 when ``nice`` exists (else ``lower_priority`` after the start)."""
    nice = shutil.which("nice")
    return [nice, "-n", "10", *argv] if nice else list(argv)


def spawn(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    """Start ``argv`` in its own session (so its own process group), stdout piped, no
    stdin, no inherited file descriptors. Raises OSError when it can't start."""
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            env=env, cwd=cwd, close_fds=True, start_new_session=True)


def cap_memory(pid: int, limit: int) -> None:
    """``RLIMIT_AS`` of a running process; OSError or ValueError when it can't be set
    (the process may already have exited)."""
    import resource

    resource.prlimit(pid, resource.RLIMIT_AS, (limit, limit))


def lower_priority(pid: int) -> None:
    """Nice 10 for a process started without ``nice``; best effort."""
    with contextlib.suppress(OSError):
        os.setpriority(os.PRIO_PROCESS, pid, 10)


# ---------------------------------------------------------------- exit, no thread
def watch_exit(loop: asyncio.AbstractEventLoop, proc: subprocess.Popen[bytes],
               fut: asyncio.Future[int]) -> None:
    """Resolve ``fut`` with the exit code when ``proc`` exits, reaping it. Uses a pidfd
    on the event loop (Linux 5.3+): no thread. Without pidfd: a 0.5 s check on the loop,
    only while the process runs."""
    def reap() -> bool:
        code = proc.poll()
        if code is None:
            return False
        if not fut.done():
            fut.set_result(code)
        return True

    try:
        pidfd = os.pidfd_open(proc.pid)
    except (AttributeError, OSError):
        async def poll() -> None:
            while not reap():
                await asyncio.sleep(0.5)
        task = loop.create_task(poll())
        fut.add_done_callback(lambda _: task.cancel())
        return

    # the done callback removes the reader and closes the pidfd, exactly once
    loop.add_reader(pidfd, reap)
    fut.add_done_callback(lambda _: close_pidfd(loop, pidfd))
    reap()  # it may have exited before the reader was added


def close_pidfd(loop: asyncio.AbstractEventLoop, pidfd: int) -> None:
    with contextlib.suppress(Exception):
        loop.remove_reader(pidfd)
    with contextlib.suppress(OSError):
        os.close(pidfd)


def unwatch_exit(loop: asyncio.AbstractEventLoop, fut: asyncio.Future[int]) -> None:
    if not fut.done():
        fut.cancel()  # its done callback closes the pidfd


async def run_quick(argv: list[str], env: dict[str, str], timeout: float = 5.0) -> int:
    """Run a short helper (systemd probe, scope stop) without blocking the loop or
    starting a thread; its exit code, or -1."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, close_fds=True)
    except OSError:
        return -1
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[int] = loop.create_future()
    watch_exit(loop, proc, fut)
    try:
        return await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        proc.kill()
        proc.wait()
        return -1


# ---------------------------------------------------------------- output
class LineSplitter:
    """Bytes in, complete lines out. A line longer than ``limit`` is dropped whole,
    up to and including its newline, so its tail is never read as a line of its own."""

    def __init__(self, limit: int = MAX_LINE) -> None:
        self.limit, self.buf, self.skipping = limit, b"", False

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        lines = []
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > self.limit:  # too long already: drop until its end
                    self.buf, self.skipping = b"", True
                return lines
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            if self.skipping:
                self.skipping = False
            elif len(line) <= self.limit:
                lines.append(line)

    def end(self) -> list[bytes]:
        """At EOF: a last line without a newline, unless it is being dropped."""
        line, self.buf = self.buf, b""
        return [line] if line and not self.skipping and len(line) <= self.limit else []


async def read_lines(pipe: object, on_line: Callable[[str], None]) -> None:
    """Read ``pipe`` (a child's stdout) on the loop until EOF, calling ``on_line`` with
    each complete line (UTF-8, errors replaced; over-long lines dropped)."""
    loop = asyncio.get_running_loop()
    stream = asyncio.StreamReader(limit=2 * MAX_LINE, loop=loop)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stream, loop=loop), pipe)
    lines = LineSplitter()
    try:
        while True:
            data = await stream.read(MAX_LINE)
            for raw in lines.feed(data) if data else lines.end():
                on_line(raw.decode("utf-8", "replace"))
            if not data:
                return
    finally:
        transport.close()


# ---------------------------------------------------------------- stop
def kill_group(pid: int) -> None:
    """SIGKILL the process group ``pid`` leads (helpers the child left there too)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def terminate(proc: subprocess.Popen[bytes], exited: asyncio.Future[int],
                    grace: float, on_kill: Callable[[], None] | None = None) -> None:
    """SIGTERM ``proc``; if ``exited`` isn't resolved within ``grace`` seconds, call
    ``on_kill`` and SIGKILL its process group. Returns once it exited."""
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(exited), grace)
    except TimeoutError:
        if on_kill is not None:
            on_kill()
        kill_group(proc.pid)
        await asyncio.shield(exited)


def alive(pid: int | None, marker: str) -> bool:
    """Is ``pid`` still the process whose argv has ``marker`` (not a reused pid)?"""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:  # no /proc: only ask whether the pid exists
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return marker.encode() in cmdline.split(b"\0")


async def stop_pid(pid: int, marker: str, grace: float) -> None:
    """Stop a process this server didn't start (no exit to await): SIGTERM, then SIGKILL
    to its process group if it is still there after ``grace`` seconds."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while alive(pid, marker) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if alive(pid, marker):
        kill_group(pid)


def killed_by_runtime_limit(code: int, ran_for: float, max_seconds: float) -> bool:
    """Was the process stopped by an outside runtime limit (systemd's
    ``RuntimeMaxSec``): killed by SIGTERM or SIGKILL after its max duration had passed?"""
    return code in (-signal.SIGTERM, -signal.SIGKILL) and ran_for >= max_seconds


def is_pid_namespace_init() -> bool:
    """Is this process its PID namespace's init (PID 1 in a container)? When it exits,
    the kernel SIGKILLs every other process of the namespace, children included."""
    return os.getpid() == 1
