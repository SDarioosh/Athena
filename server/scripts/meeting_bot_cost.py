#!/usr/bin/env python3
"""Idle and cost check of Athena's Zoom meeting bot (docs/zoom-meeting-bot-plan.md,
"Resource rules", lane 5C). Standard library only: it runs in CI without the server's
virtualenv, and inside the sound-api container.

Two modes:

Fixture (default): runs ``athena-meeting-bot`` built against the sandbox stub SDK
(``meeting-bot/bot/sdkstub``, scenario ``fixture``) as the launcher would (bot contract
argv and env with placeholder tokens, ``nice 10`` and ``RLIMIT_AS``, its own process
session, an empty ``--work-dir``, a private ``TMPDIR`` and ``HOME``). Dana's audio is a
fictional PCM fixture this script generates at run time (tone and noise bursts with
pauses; no audio file in git); the stub's host ends the meeting after ``--seconds``.
A local fake upload API stands in for Athena and counts chunks, the chat call and /end::

    python3 server/scripts/meeting_bot_cost.py build-bot-stub/athena-meeting-bot

Watch: attaches to the running ``athena-meeting-bot`` of a real meeting (found by name,
or ``--pid``), samples it until it exits, then runs the same leftover checks::

    python3 server/scripts/meeting_bot_cost.py --watch

Both sample the bot and every process of its session (or its descendants) from /proc
about every 0.2 s: CPU (average, and peak over 1 s windows), peak RSS (VmHWM), VmPeak
(peak address space, what the plain runner's RLIMIT_AS caps), threads, sockets. After
the bot exits it fails (exit 1) when a process of the bot is still alive, a socket it had
is still open, a file is left in its work dir or private TMPDIR, the fake uploads look
wrong (fixture), or VmPeak passes ``--max-vmpeak`` (default CONVO_MEETING_BOT_MEMORY, else
2G); it warns above 75 % of that and says what to set CONVO_MEETING_BOT_MEMORY to.
Exit 2: the check could not run (no bot found, bad arguments).

Nothing here prints a token or the bot's environment: only TMPDIR and HOME are read from
it. All data is fictional.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import json
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_NAME = "athena-meeting-bot"
DEFAULT_CAP = "2G"  # CONVO_MEETING_BOT_MEMORY's default (convo_server/config.py)
WARN_FRACTION = 0.75
SAMPLE_RATE = 32000  # the SDK's raw audio: s16le mono 32 kHz
# fictional values only: placeholders that are never real secrets
MEETING_NUMBER = "81234567890"
SESSION_ID = "zoom-costcheck"
PLACEHOLDERS = {"ATHENA_MEETING_JWT": "jwt-placeholder-cost-5c",
                "ATHENA_UPLOAD_TOKEN": "upload-placeholder-cost-5c",
                "ATHENA_MEETING_PASSCODE": "passcode-placeholder-cost-5c"}
DANA_TRACK = "16779264"  # the stub's fictional Dana Example
SELF_TRACK = "16778240"  # the stub's Athena (its own audio must never be uploaded)
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)(?:i?B)?$", re.IGNORECASE)
_SOCKET = re.compile(r"^socket:\[(\d+)\]$")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# ------------------------------------------------------------------ sizes
def parse_size(text: str) -> int:
    """``2G``, ``1536M``, ``2147483648`` (bytes; K/M/G/T are powers of 1024, as
    CONVO_MEETING_BOT_MEMORY and systemd read them)."""
    m = _MEMORY.match(text.strip())
    if not m:
        raise ValueError(f"not a size like 2G or 1536M: {text!r}")
    return int(float(m.group(1)) * 1024 ** "_KMGT".index((m.group(2) or "_").upper()))


def format_size(n: float) -> str:
    """Bytes for people: 512.0M, 1.50G."""
    for unit, power in (("G", 3), ("M", 2), ("K", 1)):
        if n >= 1024 ** power:
            return f"{n / 1024 ** power:.{2 if unit == 'G' else 1}f}{unit}"
    return f"{int(n)}B"


def suggest_cap(peak: int, headroom: float = 1.5, step: int = 256 * 1024 ** 2) -> str:
    """A CONVO_MEETING_BOT_MEMORY value with headroom over ``peak``, rounded up to 256M
    (whole gigabytes are written as ``<n>G``)."""
    want = max(step, math.ceil(peak * headroom / step) * step)
    return f"{want // 1024 ** 3}G" if want % 1024 ** 3 == 0 else f"{want // 1024 ** 2}M"


def configured_cap(env_file: Path | None) -> tuple[int, str]:
    """The memory cap the launcher uses: CONVO_MEETING_BOT_MEMORY from the environment,
    else from server/.env (only that one line is read), else 2G."""
    value, source = os.environ.get("CONVO_MEETING_BOT_MEMORY", "").strip(), "environment"
    if not value and env_file is not None:
        try:
            for line in env_file.read_text(errors="replace").splitlines():
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == "CONVO_MEETING_BOT_MEMORY":
                    value, source = val.strip().strip("\"'"), str(env_file)
        except OSError:
            pass
    if not value:
        return parse_size(DEFAULT_CAP), "default 2G"
    return parse_size(value), f"CONVO_MEETING_BOT_MEMORY={value} ({source})"


# ------------------------------------------------------------------ /proc parsing
@dataclass(frozen=True)
class Stat:
    pid: int
    comm: str
    state: str
    ppid: int
    pgrp: int
    session: int
    ticks: int  # utime + stime
    start: int  # starttime, clock ticks after boot (with pid: a unique process key)


def parse_stat(text: str) -> Stat:
    """``/proc/<pid>/stat``. The command name may hold spaces and parentheses, so the
    fields are split after its last ``)``."""
    head, _, rest = text.rpartition(")")
    pid_s, _, comm = head.partition(" (")
    f = rest.split()
    # rest[0] is field 3 (state); utime/stime are fields 14/15, starttime field 22
    return Stat(pid=int(pid_s), comm=comm, state=f[0], ppid=int(f[1]), pgrp=int(f[2]),
                session=int(f[3]), ticks=int(f[11]) + int(f[12]), start=int(f[19]))


def parse_status(text: str) -> dict[str, int]:
    """The numbers of ``/proc/<pid>/status`` we use: Vm* and Rss* in bytes, Threads.
    A zombie has no Vm lines (its memory is gone)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        parts = val.split()
        if not parts:
            continue
        if key in ("VmPeak", "VmSize", "VmHWM", "VmRSS") and len(parts) == 2:
            out[key] = int(parts[0]) * 1024
        elif key in ("Threads", "PPid", "Tgid"):
            out[key] = int(parts[0])
    return out


def parse_limit_as(text: str) -> int | None:
    """``Max address space`` of ``/proc/<pid>/limits`` in bytes; None if unlimited."""
    for line in text.splitlines():
        if line.startswith("Max address space"):
            soft = line[len("Max address space"):].split()[0]
            return None if soft == "unlimited" else int(soft)
    return None


def net_inodes(texts: Iterable[str], inode_col: int) -> set[int]:
    """Socket inodes still open in a /proc/net table (tcp, udp, unix...). TIME_WAIT
    entries have inode 0 and belong to no process: ignored."""
    out: set[int] = set()
    for text in texts:
        for line in text.splitlines()[1:]:
            cols = line.split()
            if len(cols) > inode_col and cols[inode_col].isdigit() and int(cols[inode_col]):
                out.add(int(cols[inode_col]))
    return out


def listening_inodes(tcp_texts: Iterable[str]) -> set[int]:
    """Inodes of TCP sockets in state LISTEN (0A)."""
    out: set[int] = set()
    for text in tcp_texts:
        for line in text.splitlines()[1:]:
            cols = line.split()
            if len(cols) > 9 and cols[3] == "0A" and cols[9].isdigit():
                out.add(int(cols[9]))
    return out


def _read(path: str) -> str | None:
    """A /proc file, or None when the process is gone or not ours (it may exit between
    any two reads: every caller treats None as "gone")."""
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def read_stat(pid: int) -> Stat | None:
    text = _read(f"/proc/{pid}/stat")
    if not text:
        return None
    try:
        return parse_stat(text)
    except (ValueError, IndexError):
        return None


def all_pids() -> list[int]:
    try:
        return [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []


def fd_targets(pid: int) -> list[str]:
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return []
    out = []
    for name in names:
        try:
            out.append(os.readlink(f"/proc/{pid}/fd/{name}"))
        except OSError:
            continue
    return out


def cmdline(pid: int) -> list[str]:
    text = _read(f"/proc/{pid}/cmdline")
    return [a for a in text.split("\0") if a] if text else []


def env_value(pid: int, name: str) -> str | None:
    """One variable of the bot's environment (TMPDIR, HOME). Nothing else is kept."""
    text = _read(f"/proc/{pid}/environ")
    if not text:
        return None
    for item in text.split("\0"):
        key, sep, val = item.partition("=")
        if sep and key == name:
            return val
    return None


def arg_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv[:-1]):
        if a == flag:
            return argv[i + 1]
    return None


def cgroup_memory_peak(pid: int) -> int | None:
    """memory.peak of the bot's cgroup v2 (the systemd scope's MemoryMax counts this),
    when the kernel has it and the bot has a cgroup of its own; else None."""
    text = _read(f"/proc/{pid}/cgroup")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            if not path.endswith(".scope") or "athena-meeting" not in path:
                return None  # the server's own cgroup: not the bot's alone
            value = _read(f"/sys/fs/cgroup{path}/memory.peak")
            return int(value) if value and value.strip().isdigit() else None
    return None


# ------------------------------------------------------------------ sampling
@dataclass
class Proc:
    pid: int
    start: int
    comm: str
    ticks: int = 0
    vmpeak: int = 0
    hwm: int = 0
    alive: bool = True


@dataclass
class Sample:
    t: float
    ticks: int  # cumulative clock ticks of every process seen (dead ones frozen)
    rss: int  # summed VmRSS of the live processes
    threads: int


@dataclass
class Tracker:
    """Follows one bot: its pid, every process in its session (when it leads one) and
    every descendant. Processes are keyed by (pid, starttime), so a reused pid is never
    taken for the bot."""

    root: int
    session: int | None = None
    procs: dict[tuple[int, int], Proc] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    sockets: dict[int, tuple[int, str]] = field(default_factory=dict)  # inode -> pid, comm
    listening: set[int] = field(default_factory=set)
    open_files: set[str] = field(default_factory=set)
    threads_peak: int = 0
    rss_sum_peak: int = 0
    cgroup_peak: int | None = None
    root_vmpeak: int = 0
    root_hwm: int = 0
    root_limit_as: int | None = None
    root_start: int | None = None

    def members(self, stats: dict[int, Stat]) -> list[Stat]:
        """The live processes of the bot (zombies count: they are not reaped yet)."""
        known = {pid for pid, _ in self.procs}
        out: list[Stat] = []
        for pid, st in stats.items():
            key = (pid, st.start)
            in_session = self.session is not None and st.session == self.session
            if (key in self.procs or pid == self.root or in_session or st.ppid in known
                    or st.ppid == self.root):
                out.append(st)
        # descendants of descendants seen for the first time in this scan
        changed = True
        have = {s.pid for s in out}
        while changed:
            changed = False
            for pid, st in stats.items():
                if pid not in have and st.ppid in have:
                    out.append(st)
                    have.add(pid)
                    changed = True
        return out

    def sample(self, now: float | None = None) -> bool:
        """One pass over /proc. False once no process of the bot is left (zombies
        included), so the caller can stop."""
        stats = {}
        for pid in all_pids():
            st = read_stat(pid)
            if st is not None:
                stats[pid] = st
        root = stats.get(self.root)
        if root is not None:
            if self.root_start is None:
                self.root_start = root.start
                if root.session == root.pid:  # it leads its own session (the launcher)
                    self.session = root.session
            elif root.start != self.root_start:
                stats.pop(self.root)  # the pid was reused: not ours
        live = self.members(stats)
        for p in self.procs.values():
            p.alive = False
        rss_sum = threads = 0
        tcp: list[str] | None = None
        for st in live:
            key = (st.pid, st.start)
            p = self.procs.get(key)
            if p is None:
                p = self.procs[key] = Proc(st.pid, st.start, st.comm)
            p.alive, p.ticks = True, max(p.ticks, st.ticks)
            status = parse_status(_read(f"/proc/{st.pid}/status") or "")
            p.vmpeak = max(p.vmpeak, status.get("VmPeak", 0))
            p.hwm = max(p.hwm, status.get("VmHWM", 0), status.get("VmRSS", 0))
            rss_sum += status.get("VmRSS", 0)
            threads += status.get("Threads", 0)
            if st.pid == self.root:
                self.root_vmpeak, self.root_hwm = p.vmpeak, p.hwm
                limits = _read(f"/proc/{st.pid}/limits")
                if limits is not None:
                    self.root_limit_as = parse_limit_as(limits)
                peak = cgroup_memory_peak(st.pid)
                if peak is not None:
                    self.cgroup_peak = max(self.cgroup_peak or 0, peak)
            for target in fd_targets(st.pid):
                m = _SOCKET.match(target)
                if m:
                    inode = int(m.group(1))
                    if inode not in self.sockets:
                        self.sockets[inode] = (st.pid, st.comm)
                        if tcp is None:
                            tcp = [t for t in (_read("/proc/net/tcp"),
                                               _read("/proc/net/tcp6")) if t]
                        if inode in listening_inodes(tcp):
                            self.listening.add(inode)
                elif target.startswith("/") and not target.startswith(("/dev/", "/proc/")):
                    self.open_files.add(target.removesuffix(" (deleted)"))
        self.rss_sum_peak = max(self.rss_sum_peak, rss_sum)
        self.threads_peak = max(self.threads_peak, threads)
        total = sum(p.ticks for p in self.procs.values())
        self.samples.append(Sample(time.monotonic() if now is None else now, total, rss_sum,
                                   threads))
        return bool(live)

    def leftovers(self) -> list[str]:
        """Processes of the bot still there: its session, its process group, or a
        descendant seen while it ran (same pid and start time)."""
        out = []
        for pid in all_pids():
            st = read_stat(pid)
            if st is None:
                continue
            ours = (pid, st.start) in self.procs or (
                self.session is not None and self.session in (st.session, st.pgrp))
            if ours:
                out.append(f"pid {pid} ({st.comm}, state {st.state})")
        return out


def cpu_stats(samples: list[Sample], window: float = 1.0,
              clk_tck: int = CLK_TCK) -> tuple[float, float, float]:
    """(average %, peak % over ``window``-second windows, CPU seconds) of a run; 100 %
    is one core. Windows are made from consecutive samples spanning >= ``window``."""
    if len(samples) < 2:
        return 0.0, 0.0, (samples[0].ticks / clk_tck if samples else 0.0)
    first, last = samples[0], samples[-1]
    span = last.t - first.t
    cpu_s = (last.ticks - first.ticks) / clk_tck
    avg = 100.0 * cpu_s / span if span > 0 else 0.0
    peak = 0.0
    j = 0
    for i in range(1, len(samples)):
        while j < i and samples[i].t - samples[j + 1].t >= window:
            j += 1
        dt = samples[i].t - samples[j].t
        if dt >= window * 0.5 or (i == len(samples) - 1 and peak == 0.0 and dt > 0):
            peak = max(peak, 100.0 * (samples[i].ticks - samples[j].ticks) / clk_tck / dt)
    return avg, max(peak, avg), cpu_s


# ------------------------------------------------------------------ leftovers
def files_left(root: Path | None) -> list[str]:
    """Every non-directory under ``root`` (empty directories are fine)."""
    if root is None or not root.exists():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            out.append(str(Path(dirpath, name).relative_to(root)))
        for name in dirnames:  # a symlink to a dir is a leftover too
            if Path(dirpath, name).is_symlink():
                out.append(str(Path(dirpath, name).relative_to(root)))
    return sorted(out)


def dir_bytes(root: Path | None) -> int:
    total = 0
    if root is None:
        return 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def tcp_connections_to(port: int) -> list[str]:
    """Open client-side TCP sockets (owned by a process: inode != 0, so not TIME_WAIT)
    to this loopback port. The server side belongs to this script and is not counted."""
    out = []
    for name in ("tcp", "tcp6"):
        text = _read(f"/proc/net/{name}") or ""
        for line in text.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 10 or not cols[9].isdigit() or int(cols[9]) == 0:
                continue
            if ":" in cols[2] and int(cols[2].rsplit(":", 1)[1], 16) == port:
                out.append(line.strip())
    return out


def sockets_still_open(inodes: Iterable[int]) -> list[int]:
    """Which of these socket inodes some process still holds."""
    tables = {6: ("unix", "netlink"), 9: ("tcp", "tcp6", "udp", "udp6", "raw", "raw6")}
    open_now: set[int] = set()
    for col, names in tables.items():
        open_now |= net_inodes((t for t in (_read(f"/proc/net/{n}") for n in names) if t), col)
    # netlink's inode is the last column: read it on its own
    nl = _read("/proc/net/netlink")
    if nl:
        for line in nl.splitlines()[1:]:
            cols = line.split()
            if cols and cols[-1].isdigit():
                open_now.add(int(cols[-1]))
    return sorted(set(inodes) & open_now)


# ------------------------------------------------------------------ verdict
@dataclass
class Report:
    mode: str
    seconds: float = 0.0
    exit_code: int | None = None
    status_lines: list[str] = field(default_factory=list)
    cpu_avg: float = 0.0
    cpu_peak: float = 0.0
    cpu_seconds: float = 0.0
    vmpeak: int = 0  # the bot process (RLIMIT_AS is per process)
    vmpeak_max_any: int = 0  # any process of the bot
    hwm: int = 0
    rss_sum_peak: int = 0
    cgroup_peak: int | None = None
    threads_peak: int = 0
    processes: int = 0
    sockets: int = 0
    listening: int = 0
    work_dir_peak: int = 0
    cap: int = 0
    cap_source: str = ""
    limit_as: int | None = None
    uploads: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    memory_advice: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict:
        return {"ok": self.ok, "mode": self.mode, "seconds": round(self.seconds, 1),
                "exit_code": self.exit_code, "status_lines": self.status_lines,
                "cpu": {"avg_percent": round(self.cpu_avg, 1),
                        "peak_percent": round(self.cpu_peak, 1),
                        "cpu_seconds": round(self.cpu_seconds, 2)},
                "memory": {"vmpeak_bytes": self.vmpeak, "vmpeak_any_bytes": self.vmpeak_max_any,
                           "vmhwm_bytes": self.hwm, "rss_sum_peak_bytes": self.rss_sum_peak,
                           "cgroup_peak_bytes": self.cgroup_peak, "cap_bytes": self.cap,
                           "cap_source": self.cap_source, "rlimit_as_bytes": self.limit_as,
                           "advice": self.memory_advice},
                "threads_peak": self.threads_peak, "processes": self.processes,
                "sockets": self.sockets, "listening_sockets": self.listening,
                "work_dir_peak_bytes": self.work_dir_peak, "uploads": self.uploads,
                "problems": self.problems, "warnings": self.warnings}


def memory_verdict(vmpeak: int, cap: int, *, limit_as: int | None = None,
                   cgroup_peak: int | None = None) -> tuple[str, str]:
    """(level, advice) with level ok / warn / fail. The plain runner caps address space
    (RLIMIT_AS): VmPeak is what counts. Under a systemd scope (no RLIMIT_AS) MemoryMax caps
    the scope's memory: its cgroup peak (or the RSS) is what counts there, and VmPeak is
    reported only."""
    if limit_as is None and cgroup_peak is not None:
        measured, what = cgroup_peak, "the scope's memory peak (MemoryMax)"
    else:
        measured = vmpeak
        what = ("VmPeak (RLIMIT_AS)" if limit_as is not None
                else "VmPeak (what the plain runner's RLIMIT_AS would cap)")
    if limit_as is not None:
        cap = limit_as
    share = measured / cap if cap else 0.0
    if measured > cap:
        return "fail", (f"{what} {format_size(measured)} is over the cap {format_size(cap)}: "
                        f"raise CONVO_MEETING_BOT_MEMORY to {suggest_cap(measured)}")
    if share > WARN_FRACTION:
        return "warn", (f"{what} {format_size(measured)} is {share:.0%} of the cap "
                        f"{format_size(cap)}: raise CONVO_MEETING_BOT_MEMORY to "
                        f"{suggest_cap(measured)}")
    return "ok", (f"{what} {format_size(measured)} is {share:.0%} of the cap "
                  f"{format_size(cap)}: CONVO_MEETING_BOT_MEMORY needs no change")


def check_uploads(requests: list[dict]) -> tuple[dict[str, int], list[str]]:
    """What the fake upload API got in a fixture run: Dana's chunks, one chat, one /end,
    all with the upload token; never the bot's own track."""
    chunks = [r for r in requests if r["path"] == "/v1/ingest/chunk"]
    chat = [r for r in requests if r["path"] == f"/v1/sessions/{SESSION_ID}/chat"]
    end = [r for r in requests if r["path"] == f"/v1/sessions/{SESSION_ID}/end"]
    dana = [r for r in chunks if DANA_TRACK.encode() in r["body"]]
    counts = {"chunks": len(chunks), "dana_chunks": len(dana), "chat": len(chat),
              "end": len(end), "bytes": sum(len(r["body"]) for r in chunks)}
    problems = []
    if not dana:
        problems.append("no audio chunk on Dana's track reached the upload API")
    if any(SELF_TRACK.encode() in r["body"] for r in chunks):
        problems.append("the bot uploaded its own audio")
    if len(end) != 1:
        problems.append(f"{len(end)} /end calls (want 1)")
    if len(chat) != 1:
        problems.append(f"{len(chat)} chat calls (want 1)")
    token = PLACEHOLDERS["ATHENA_UPLOAD_TOKEN"]
    if any(r.get("auth") != f"Bearer {token}" for r in requests):
        problems.append("an upload came without the meeting's upload token")
    return counts, problems


def finish_report(rep: Report, tr: Tracker, *, leftover_procs: list[str],
                  open_sockets: list[int], work_left: list[str], tmp_left: list[str],
                  tmp_label: str = "private TMPDIR") -> Report:
    """Fills the numbers from the tracker and turns leftovers into problems."""
    rep.cpu_avg, rep.cpu_peak, rep.cpu_seconds = cpu_stats(tr.samples)
    rep.vmpeak = tr.root_vmpeak
    rep.vmpeak_max_any = max((p.vmpeak for p in tr.procs.values()), default=0)
    rep.hwm = tr.root_hwm
    rep.rss_sum_peak, rep.cgroup_peak = tr.rss_sum_peak, tr.cgroup_peak
    rep.threads_peak, rep.processes = tr.threads_peak, len(tr.procs)
    rep.sockets, rep.listening = len(tr.sockets), len(tr.listening)
    rep.limit_as = tr.root_limit_as
    if leftover_procs:
        rep.problems.append("bot processes still running after it exited: "
                            + ", ".join(leftover_procs))
    if open_sockets:
        rep.problems.append(f"{len(open_sockets)} socket(s) the bot had are still open "
                            f"(inodes {', '.join(map(str, open_sockets[:10]))})")
    if work_left:
        rep.problems.append("files left in the work dir: " + ", ".join(work_left[:10]))
    if tmp_left:
        rep.problems.append(f"files left in the {tmp_label}: " + ", ".join(tmp_left[:10]))
    if tr.listening:
        rep.warnings.append(f"the bot listened on {len(tr.listening)} TCP socket(s); it "
                            "should only connect out")
    if rep.vmpeak == 0:
        rep.warnings.append("no memory sample of the bot (did it exit at once?)")
    level, rep.memory_advice = memory_verdict(max(rep.vmpeak, rep.vmpeak_max_any), rep.cap,
                                              limit_as=rep.limit_as,
                                              cgroup_peak=rep.cgroup_peak)
    if level == "fail":
        rep.problems.append(rep.memory_advice)
    elif level == "warn":
        rep.warnings.append(rep.memory_advice)
    return rep


def render(rep: Report) -> str:
    def row(k: str, v: str) -> str:
        return f"  {k:<26} {v}"

    cap_note = (f"RLIMIT_AS {format_size(rep.limit_as)}" if rep.limit_as
                else "no RLIMIT_AS (systemd scope or uncapped)")
    lines = [f"meeting bot cost check ({rep.mode}): {'OK' if rep.ok else 'FAILED'}",
             row("ran", f"{rep.seconds:.1f} s, exit code "
                 + (str(rep.exit_code) if rep.exit_code is not None else "unknown (watched)")),
             row("CPU (100% = one core)",
                 f"avg {rep.cpu_avg:.1f}%, peak {rep.cpu_peak:.1f}% (1 s windows), "
                 f"{rep.cpu_seconds:.2f} CPU s"),
             row("VmPeak (address space)", f"{format_size(rep.vmpeak)}"
                 + (f" (largest process {format_size(rep.vmpeak_max_any)})"
                    if rep.vmpeak_max_any > rep.vmpeak else "")),
             row("peak RSS (VmHWM)", f"{format_size(rep.hwm)}; all processes "
                 f"{format_size(rep.rss_sum_peak)}"),
             ]
    if rep.cgroup_peak is not None:
        lines.append(row("scope memory.peak", format_size(rep.cgroup_peak)))
    lines += [row("memory cap", f"{format_size(rep.cap)} from {rep.cap_source}; {cap_note}"),
              row("threads (peak)", str(rep.threads_peak)),
              row("processes / sockets", f"{rep.processes} / {rep.sockets} "
                  f"({rep.listening} listening)"),
              row("work dir peak", format_size(rep.work_dir_peak))]
    if rep.uploads:
        lines.append(row("uploads", ", ".join(f"{k} {v}" for k, v in rep.uploads.items())))
    if rep.status_lines:
        lines.append(row("status lines", " | ".join(rep.status_lines)))
    level = ("FAIL" if rep.memory_advice in rep.problems
             else "WARN" if rep.memory_advice in rep.warnings else "ok  ")
    lines.append(f"{level} memory: {rep.memory_advice}")
    lines += [f"WARN {w}" for w in rep.warnings if w != rep.memory_advice]
    lines += [f"FAIL {p}" for p in rep.problems if p != rep.memory_advice]
    return "\n".join(lines)


# ------------------------------------------------------------------ fixture
def make_fixture(seconds: float = 12.0, seed: int = 5) -> bytes:
    """Fictional "speech" for Dana's track: s16le mono 32 kHz, bursts of a voiced tone
    (110-220 Hz with harmonics, a syllable-rate envelope and a little noise) between
    pauses. Deterministic for a seed (its own LCG, not ``random``)."""
    state = seed & 0xFFFFFFFF or 1

    def rnd() -> float:  # [0, 1)
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return state / 2 ** 32

    total = int(seconds * SAMPLE_RATE)
    pcm = array.array("h", bytes(2 * total))
    i = 0
    talking = True
    while i < total:
        length = int((0.6 + 1.8 * rnd() if talking else 0.3 + 1.0 * rnd()) * SAMPLE_RATE)
        end = min(total, i + length)
        if talking:
            f0 = 110.0 + 110.0 * rnd()
            syll = 3.0 + 3.0 * rnd()
            for n in range(i, end):
                t = (n - i) / SAMPLE_RATE
                env = 0.5 - 0.5 * math.cos(2 * math.pi * syll * t)
                v = (math.sin(2 * math.pi * f0 * t) + 0.5 * math.sin(4 * math.pi * f0 * t)
                     + 0.25 * math.sin(6 * math.pi * f0 * t))
                pcm[n] = int(max(-32767, min(32767, 7000 * env * v + 400 * (rnd() - 0.5))))
        else:
            for n in range(i, end):
                pcm[n] = int(60 * (rnd() - 0.5))
        i, talking = end, not talking
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


class _Api(BaseHTTPRequestHandler):
    requests: list[dict]
    lock: threading.Lock

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.lock:
            self.requests.append({"path": self.path.split("?")[0],
                                  "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"ok": True, "stop": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: object) -> None:
        pass


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def run_fixture(bot: Path, seconds: int, cap: int, cap_source: str, interval: float,
                fixture_seconds: float) -> Report:
    rep = Report(mode="fixture", cap=cap, cap_source=cap_source)
    base = Path(tempfile.mkdtemp(prefix="athena-cost-"))
    server: ThreadingHTTPServer | None = None
    proc: subprocess.Popen[str] | None = None
    try:
        fixture_dir, work, tmp, home = (base / n for n in ("fixture", "work", "tmp", "home"))
        for d in (fixture_dir, work, tmp, home):
            d.mkdir(mode=0o700)
        pcm = fixture_dir / "dana.s16le"
        pcm.write_bytes(make_fixture(fixture_seconds))

        handler = type("Api", (_Api,), {"requests": [], "lock": threading.Lock()})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"

        args = ["--meeting-id", MEETING_NUMBER, "--session-id", SESSION_ID,
                "--upload-url", url, "--name", "Athena", "--max-seconds", str(seconds + 120),
                "--alone-seconds", "60", "--waiting-room-seconds", "30", "--codec", "opus",
                "--work-dir", str(work)]
        nice = shutil.which("nice")
        argv = ([nice, "-n", "10"] if nice else []) + [str(bot), *args]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "TMPDIR": str(tmp), "LANG": "C.UTF-8", **PLACEHOLDERS,
               "ATHENA_SDKSTUB_SCENARIO": "fixture", "ATHENA_SDKSTUB_PCM": str(pcm),
               "ATHENA_SDKSTUB_SECONDS": str(seconds)}
        started = time.monotonic()
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=work, text=True,
                                close_fds=True, start_new_session=True)
        try:  # the launcher's plain runner: RLIMIT_AS right after the start
            resource.prlimit(proc.pid, resource.RLIMIT_AS, (cap, cap))
        except (OSError, ValueError) as e:
            rep.warnings.append(f"could not set RLIMIT_AS: {e}")
        out_lines: list[str] = []
        err: list[str] = []
        readers = [threading.Thread(target=lambda: out_lines.extend(
                       line.rstrip("\n") for line in proc.stdout), daemon=True),
                   threading.Thread(target=lambda: err.append(proc.stderr.read()),
                                    daemon=True)]
        for t in readers:
            t.start()

        tr = Tracker(root=proc.pid)
        deadline = started + seconds + 60
        timed_out = False
        while proc.poll() is None:
            tr.sample()
            rep.work_dir_peak = max(rep.work_dir_peak, dir_bytes(work))
            if time.monotonic() > deadline:
                timed_out = True
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(interval)
        code = proc.wait()
        rep.seconds = time.monotonic() - started
        tr.sample()  # the final tick count of anything still there
        for t in readers:
            t.join(5)
        rep.exit_code = code
        rep.status_lines = [line.removeprefix("STATE ") for line in out_lines
                            if line.startswith("STATE ")]
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)

        # leftovers: give helpers a moment to exit on their own, as the launcher would
        grace = time.monotonic() + 3
        left = tr.leftovers()
        while left and time.monotonic() < grace:
            time.sleep(0.1)
            left = tr.leftovers()
        still = sockets_still_open(tr.sockets)
        port = server.server_address[1]
        server.shutdown()
        server.server_close()
        # uploads are short: a connection to the fake API still open now is the bot's
        # (or a helper's), since the API's own side closed with it
        conns = tcp_connections_to(port)
        counts, upload_problems = check_uploads(handler.requests)
        rep.uploads = counts
        finish_report(rep, tr, leftover_procs=left, open_sockets=still,
                      work_left=files_left(work), tmp_left=files_left(tmp) + [
                          f"HOME/{f}" for f in files_left(home)])
        rep.hwm = max(rep.hwm, ru.ru_maxrss * 1024)  # the kernel's own peak, if larger
        if conns:
            rep.problems.append(f"{len(conns)} connection(s) to the upload API still open "
                                "after the bot exited")
        if left:
            _kill_group(proc.pid)
        if timed_out:
            rep.problems.append(f"the bot did not end within {seconds + 60} s")
        if code != 0:
            rep.problems.append(f"the bot exited with {code} (want 0)")
        if not rep.status_lines or rep.status_lines[-1] != "ended meeting_ended":
            rep.problems.append(f"last status line {rep.status_lines[-1:]} != "
                                "['ended meeting_ended']")
        rep.problems += upload_problems
        printed = "\n".join(out_lines) + "".join(err)
        if any(v in printed for v in PLACEHOLDERS.values()):
            rep.problems.append("the bot printed a secret")
        if code != 0 and err:
            tail = "".join(err).strip().splitlines()[-5:]
            rep.warnings.append("bot stderr (last lines): " + " | ".join(tail))
        return rep
    finally:
        if proc is not None and proc.poll() is None:
            _kill_group(proc.pid)
            proc.wait()
        if server is not None:
            server.server_close()
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------------------ watch
def find_bots() -> list[int]:
    """Running athena-meeting-bot processes (the launcher's start script execs it, as do
    nice and systemd-run, so its own argv[0] names it)."""
    out = []
    for pid in all_pids():
        argv = cmdline(pid)
        if argv and os.path.basename(argv[0]) == BOT_NAME:
            out.append(pid)
    return sorted(out)


def run_watch(pid: int | None, wait_start: float, cap: int, cap_source: str,
              interval: float, work_root: Path | None) -> Report:
    rep = Report(mode="watch", cap=cap, cap_source=cap_source)
    deadline = time.monotonic() + wait_start
    while pid is None:
        bots = find_bots()
        if len(bots) > 1:
            raise SystemExit(f"several {BOT_NAME} processes ({bots}); pick one with --pid")
        if bots:
            pid = bots[0]
            break
        if time.monotonic() >= deadline:
            raise SystemExit(f"no {BOT_NAME} is running. Send Athena to a meeting first, or "
                             "wait longer with --wait-start SECONDS.")
        time.sleep(1.0)
    argv = cmdline(pid)
    if not argv:
        raise SystemExit(f"pid {pid} is gone (or not readable by this user)")
    work = arg_value(argv, "--work-dir")
    work_dir = Path(work) if work else None
    if work_root is None and work_dir is not None:
        work_root = work_dir.parent
    tmp = Path(env_value(pid, "TMPDIR") or "/tmp")
    home = env_value(pid, "HOME")
    st = read_stat(pid)
    boot_offset = st.start / CLK_TCK if st else 0.0
    print(f"watching {BOT_NAME} pid {pid} (session {arg_value(argv, '--session-id')}); "
          "it is sampled until it exits. Ctrl-C stops watching (the bot keeps running).",
          file=sys.stderr, flush=True)
    tr = Tracker(root=pid)
    started = time.monotonic()
    try:
        while tr.sample():
            if work_dir is not None:
                rep.work_dir_peak = max(rep.work_dir_peak, dir_bytes(work_dir))
            time.sleep(interval)
    except KeyboardInterrupt:
        rep.warnings.append("stopped watching before the bot exited: no leftover check")
        finish_report(rep, tr, leftover_procs=[], open_sockets=[], work_left=[], tmp_left=[])
        rep.seconds = time.monotonic() - started
        return rep
    rep.seconds = time.monotonic() - started
    # the launcher kills the bot's process group, then removes the work dir (after up to
    # 2 s for its last status lines): wait for that before judging
    grace = time.monotonic() + 15
    left = tr.leftovers()
    while time.monotonic() < grace and (left or (work_dir and work_dir.exists())):
        time.sleep(0.25)
        left = tr.leftovers()
    work_left = files_left(work_dir)
    stale = []
    if work_root is not None and work_root.is_dir():
        running = {Path(w).name for p in find_bots() if (w := arg_value(cmdline(p), "--work-dir"))}
        stale = [d.name for d in work_root.iterdir() if d.name not in running
                 and (work_dir is None or d.name != work_dir.name)]
    # files the bot had open under TMPDIR or HOME that are still there
    tmp_left = sorted(f for f in tr.open_files
                      if (f.startswith(str(tmp) + "/") or (home and f.startswith(home + "/")))
                      and os.path.exists(f) and not os.path.isdir(f))
    new_tmp = new_files_since(tmp, boot_offset)
    finish_report(rep, tr, leftover_procs=left, open_sockets=sockets_still_open(tr.sockets),
                  work_left=work_left, tmp_left=tmp_left,
                  tmp_label="TMPDIR/HOME (files it had open)")
    if stale:
        rep.warnings.append(f"other dirs under the meetings work root {work_root}: "
                            + ", ".join(sorted(stale)[:10]) + " (a meeting not running now)")
    others = [f for f in new_tmp if f not in tmp_left]
    if others:
        rep.warnings.append(f"new files in {tmp} since the bot started (maybe not the bot's): "
                            + ", ".join(others[:10]))
    return rep


def boot_time() -> float:
    text = _read("/proc/stat") or ""
    for line in text.splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def new_files_since(root: Path, start_after_boot: float, limit: int = 2000) -> list[str]:
    """Files under ``root`` (3 levels) changed after the bot started."""
    since = boot_time() + start_after_boot
    out: list[str] = []
    if not since or not root.is_dir():
        return out
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base_depth >= 3:
            dirnames[:] = []
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.lstat(path).st_ctime >= since:
                    out.append(path)
            except OSError:
                continue
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)


# ------------------------------------------------------------------ main
def default_env_file() -> Path | None:
    p = Path(__file__).resolve().parents[1] / ".env"
    return p if p.is_file() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Idle and cost check of athena-meeting-bot (fixture run or --watch).")
    ap.add_argument("bot", nargs="?", type=Path,
                    help="athena-meeting-bot built against the sandbox stub SDK (fixture mode)")
    ap.add_argument("--watch", action="store_true",
                    help="attach to the running bot of a real meeting instead")
    ap.add_argument("--pid", type=int, help="with --watch: the bot's pid")
    ap.add_argument("--wait-start", type=float, default=300.0,
                    help="with --watch: wait this long for a bot to start (default 300 s)")
    ap.add_argument("--work-root", type=Path,
                    help="with --watch: the meetings work root (default: the parent of the "
                         "bot's --work-dir, <CONVO_DATA_DIR>/meetings)")
    ap.add_argument("--seconds", type=int, default=20,
                    help="fixture: the stub's host ends the meeting after this (default 20)")
    ap.add_argument("--fixture-seconds", type=float, default=12.0,
                    help="fixture: length of the generated PCM, looped (default 12)")
    ap.add_argument("--max-vmpeak", help="memory cap, e.g. 2G (default: "
                    "CONVO_MEETING_BOT_MEMORY from the environment or server/.env, else 2G)")
    ap.add_argument("--env-file", type=Path, default=None,
                    help="where to read CONVO_MEETING_BOT_MEMORY (default server/.env)")
    ap.add_argument("--interval", type=float, default=0.2, help="sampling interval (s)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args(argv)

    if not Path("/proc/self/stat").exists():
        print("this check needs Linux /proc", file=sys.stderr)
        return 2
    try:
        if a.max_vmpeak:
            cap, source = parse_size(a.max_vmpeak), f"--max-vmpeak {a.max_vmpeak}"
        else:
            cap, source = configured_cap(a.env_file or default_env_file())
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    interval = min(max(a.interval, 0.05), 5.0)
    try:
        if a.watch:
            rep = run_watch(a.pid, a.wait_start, cap, source, interval, a.work_root)
        else:
            if a.bot is None or not os.access(a.bot, os.X_OK):
                print("give the path of athena-meeting-bot built against the stub SDK "
                      "(or --watch for a real meeting)", file=sys.stderr)
                return 2
            rep = run_fixture(a.bot.resolve(), max(1, a.seconds), cap, source, interval,
                              max(1.0, a.fixture_seconds))
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 2
        raise
    print(json.dumps(rep.as_json(), indent=2) if a.json else render(rep))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
