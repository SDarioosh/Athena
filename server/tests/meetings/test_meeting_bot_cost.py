"""The meeting bot's idle and cost check (server/scripts/meeting_bot_cost.py, lane 5C).

The pure parts run everywhere; the full fixture run needs athena-meeting-bot built against
the sandbox stub SDK (ATHENA_MEETING_BOT_STUB, or build-bot-stub/ at the repo root, as CI
builds it) and is skipped without it. Fictional data only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[2]
sys.path.insert(0, str(SERVER / "scripts"))

import meeting_bot_cost as cost  # noqa: E402

STATUS = """Name:\tathena-meeting-
Umask:\t0022
State:\tS (sleeping)
Tgid:\t4242
Pid:\t4242
PPid:\t1
VmPeak:\t  174312 kB
VmSize:\t  170000 kB
VmHWM:\t   25200 kB
VmRSS:\t   24000 kB
Threads:\t3
"""


def test_parse_status() -> None:
    s = cost.parse_status(STATUS)
    assert s["VmPeak"] == 174312 * 1024 and s["VmHWM"] == 25200 * 1024
    assert s["VmRSS"] == 24000 * 1024 and s["Threads"] == 3 and s["PPid"] == 1
    # a zombie has no Vm lines
    assert "VmPeak" not in cost.parse_status("Name:\tx\nState:\tZ (zombie)\nThreads:\t1\n")
    assert cost.parse_status("") == {}


def test_parse_stat_with_odd_names() -> None:
    rest = " ".join(["S", "1", "4242", "4242"] + ["0"] * 7 + ["150", "37"] + ["0"] * 6
                    + ["987654"] + ["0"] * 20)
    st = cost.parse_stat(f"4243 (a) b (c)) {rest}")
    assert (st.pid, st.comm, st.state, st.ppid, st.pgrp, st.session) == \
        (4243, "a) b (c)", "S", 1, 4242, 4242)
    assert st.ticks == 187 and st.start == 987654
    me = cost.read_stat(os.getpid())
    assert me is not None and me.pid == os.getpid() and me.start > 0
    assert cost.read_stat(2 ** 22 + 12345) is None  # gone mid-read: None, no error


def test_limits_and_net_tables() -> None:
    limits = ("Limit                     Soft Limit           Hard Limit           Units\n"
              "Max address space         2147483648           2147483648           bytes\n")
    assert cost.parse_limit_as(limits) == 2 * 1024 ** 3
    assert cost.parse_limit_as(limits.replace("2147483648", "unlimited")) is None
    tcp = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
           "   uid  timeout inode\n"
           "   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
           "  1000        0 111 1 0 100 0 0 10 0\n"
           "   1: 0100007F:9C40 0100007F:1F40 01 00000000:00000000 00:00000000 00000000"
           "  1000        0 222 1 0 100 0 0 10 0\n"
           "   2: 0100007F:9C41 0100007F:1F40 06 00000000:00000000 00:00000000 00000000"
           "     0        0 0 3 0\n")
    assert cost.net_inodes([tcp], 9) == {111, 222}  # TIME_WAIT (inode 0) belongs to nobody
    assert cost.listening_inodes([tcp]) == {111}


def test_sizes_and_suggestion() -> None:
    assert cost.parse_size("2G") == 2 * 1024 ** 3
    assert cost.parse_size("1536M") == 1536 * 1024 ** 2
    assert cost.parse_size("1.5G") == int(1.5 * 1024 ** 3)
    assert cost.parse_size("4096") == 4096
    with pytest.raises(ValueError):
        cost.parse_size("2 GB please")
    assert cost.format_size(170 * 1024 ** 2) == "170.0M"
    assert cost.format_size(3 * 1024 ** 3) == "3.00G"
    # 1.5x headroom, rounded up to 256M; whole gigabytes as <n>G
    assert cost.suggest_cap(1024 ** 3) == "1536M"
    assert cost.suggest_cap(int(1.9 * 1024 ** 3)) == "3G"
    assert cost.suggest_cap(1) == "256M"
    assert cost._MEMORY.match(cost.suggest_cap(int(2.7 * 1024 ** 3)))


def test_configured_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CONVO_MEETING_BOT_MEMORY", raising=False)
    assert cost.configured_cap(None) == (2 * 1024 ** 3, "default 2G")
    env = tmp_path / ".env"
    env.write_text("CONVO_INGEST_TOKEN=placeholder-not-read\nCONVO_MEETING_BOT_MEMORY=3G\n")
    cap, source = cost.configured_cap(env)
    assert cap == 3 * 1024 ** 3 and "3G" in source and "placeholder" not in source
    monkeypatch.setenv("CONVO_MEETING_BOT_MEMORY", "1536M")
    assert cost.configured_cap(env)[0] == 1536 * 1024 ** 2


def test_cpu_math() -> None:
    tck = 100
    # one core busy for 2 s of 4 s: 50 % average, 100 % peak over a 1 s window
    ticks = [0] * 4 + [25 * k for k in range(1, 9)] + [200] * 5
    samples = [cost.Sample(t=i * 0.25, ticks=v, rss=0, threads=1) for i, v in enumerate(ticks)]
    avg, peak, cpu_s = cost.cpu_stats(samples, window=1.0, clk_tck=tck)
    assert cpu_s == 2.0 and avg == pytest.approx(50.0) and peak == pytest.approx(100.0)
    assert cost.cpu_stats([], clk_tck=tck) == (0.0, 0.0, 0.0)
    one = [cost.Sample(0.0, 30, 0, 1)]
    assert cost.cpu_stats(one, clk_tck=tck) == (0.0, 0.0, 0.3)
    # shorter than a window: the whole run is the one window
    two = [cost.Sample(0.0, 0, 0, 1), cost.Sample(0.4, 10, 0, 1)]
    assert cost.cpu_stats(two, window=1.0, clk_tck=tck)[1] == pytest.approx(25.0)


def test_memory_verdict() -> None:
    g = 1024 ** 3
    level, advice = cost.memory_verdict(int(0.5 * g), 2 * g)
    assert level == "ok" and "needs no change" in advice
    level, advice = cost.memory_verdict(int(1.6 * g), 2 * g)
    assert level == "warn" and "raise CONVO_MEETING_BOT_MEMORY to 2560M" in advice
    level, advice = cost.memory_verdict(int(2.5 * g), 2 * g)
    assert level == "fail" and "to 3840M" in advice
    # the bot's real RLIMIT_AS wins over the configured value
    assert cost.memory_verdict(int(1.6 * g), 2 * g, limit_as=4 * g)[0] == "ok"
    # under a systemd scope (no RLIMIT_AS) the scope's memory peak counts, not VmPeak
    level, advice = cost.memory_verdict(5 * g, 2 * g, cgroup_peak=int(0.4 * g))
    assert level == "ok" and "MemoryMax" in advice


def test_files_left(tmp_path) -> None:
    assert cost.files_left(tmp_path / "missing") == [] and cost.files_left(None) == []
    (tmp_path / "queue" / "deep").mkdir(parents=True)
    assert cost.files_left(tmp_path) == []  # empty dirs are fine
    (tmp_path / "queue" / "16779264-000001.opus.part").write_bytes(b"x")
    (tmp_path / "chat.jsonl").write_text("{}\n")
    (tmp_path / "link").symlink_to(tmp_path / "queue")
    assert cost.files_left(tmp_path) == ["chat.jsonl", "link", "queue/16779264-000001.opus.part"]
    assert cost.dir_bytes(tmp_path) >= 4


def _tracker_with(samples: int = 3) -> cost.Tracker:
    tr = cost.Tracker(root=1)
    tr.samples = [cost.Sample(i * 0.2, i * 2, 1000, 3) for i in range(samples)]
    tr.root_vmpeak, tr.root_hwm, tr.root_limit_as = 170 * 1024 ** 2, 25 * 1024 ** 2, 2 * 1024 ** 3
    tr.procs[(1, 5)] = cost.Proc(1, 5, "athena-meeting-", vmpeak=tr.root_vmpeak)
    return tr


def test_report_verdict_and_json() -> None:
    clean = cost.finish_report(cost.Report("fixture", cap=2 * 1024 ** 3, cap_source="t"),
                               _tracker_with(), leftover_procs=[], open_sockets=[],
                               work_left=[], tmp_left=[])
    assert clean.ok and not clean.warnings
    data = clean.as_json()
    assert data["ok"] and data["memory"]["vmpeak_bytes"] == 170 * 1024 ** 2
    assert json.loads(json.dumps(data)) == data
    assert "OK" in cost.render(clean)

    capped = _tracker_with()
    capped.root_limit_as = None  # the configured cap counts
    bad = cost.finish_report(cost.Report("fixture", cap=150 * 1024 ** 2, cap_source="t"),
                             capped, leftover_procs=["pid 9 (sleep, state S)"],
                             open_sockets=[123], work_left=["chat.jsonl"], tmp_left=["leak"])
    text = " | ".join(bad.problems)
    for want in ("still running", "socket", "work dir", "TMPDIR", "over the cap"):
        assert want in text, want
    assert "FAILED" in cost.render(bad)


def test_check_uploads() -> None:
    auth = f"Bearer {cost.PLACEHOLDERS['ATHENA_UPLOAD_TOKEN']}"
    chunk = {"path": "/v1/ingest/chunk", "auth": auth,
             "body": b"track 16779264 track_name Dana Example audio"}
    chat = {"path": f"/v1/sessions/{cost.SESSION_ID}/chat", "auth": auth, "body": b"{}"}
    end = {"path": f"/v1/sessions/{cost.SESSION_ID}/end", "auth": auth, "body": b""}
    counts, problems = cost.check_uploads([chunk, chat, end])
    assert not problems and counts["dana_chunks"] == 1 and counts["end"] == 1
    _, problems = cost.check_uploads([chat])
    assert any("no audio chunk" in p for p in problems) and any("/end" in p for p in problems)
    own = dict(chunk, body=b"track 16778240")
    _, problems = cost.check_uploads([chunk, own, chat, end, dict(end, auth="Bearer x")])
    assert any("own audio" in p for p in problems)
    assert any("upload token" in p for p in problems) and any("2 /end" in p for p in problems)


def test_fixture_is_deterministic_speech_with_pauses() -> None:
    a = cost.make_fixture(4.0, seed=5)
    assert a == cost.make_fixture(4.0, seed=5)
    assert a != cost.make_fixture(4.0, seed=6)
    assert len(a) == 4 * cost.SAMPLE_RATE * 2
    import array

    pcm = array.array("h")
    pcm.frombytes(a)
    if sys.byteorder != "little":
        pcm.byteswap()
    # 100 ms blocks: some loud (speech), some near silent (pauses)
    block = cost.SAMPLE_RATE // 10
    peaks = [max(abs(v) for v in pcm[i:i + block]) for i in range(0, len(pcm), block)]
    assert max(peaks) > 5000 and min(peaks) < 100
    assert sum(p > 1000 for p in peaks) >= 10 and sum(p < 100 for p in peaks) >= 5


def test_tracker_follows_this_process_tree() -> None:
    """A real child and grandchild: both sampled, both gone afterwards."""
    p = subprocess.Popen([sys.executable, "-c",
                          "import subprocess, time; c = subprocess.Popen(['sleep', '0.6']);"
                          " time.sleep(0.4); c.wait()"], start_new_session=True)
    tr = cost.Tracker(root=p.pid)
    seen = set()
    while p.poll() is None:
        tr.sample()
        seen |= {proc.comm for proc in tr.procs.values()}
        cost.time.sleep(0.05)
    p.wait()
    assert "sleep" in seen and tr.session == p.pid
    assert tr.leftovers() == []
    assert tr.root_vmpeak > 0 and tr.threads_peak >= 1


def test_script_needs_a_bot() -> None:
    r = subprocess.run([sys.executable, str(SERVER / "scripts" / "meeting_bot_cost.py")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "athena-meeting-bot" in r.stderr
    r = subprocess.run([sys.executable, str(SERVER / "scripts" / "meeting_bot_cost.py"),
                        "--watch", "--wait-start", "0", "--pid", str(2 ** 22 + 12345)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "gone" in r.stderr


def _stub_bot() -> Path | None:
    env = os.environ.get("ATHENA_MEETING_BOT_STUB")
    root = SERVER.parent
    for p in ([Path(env)] if env else []) + [root / "build-bot-stub" / "athena-meeting-bot"]:
        if p.is_file() and os.access(p, os.X_OK) and (p.parent / "sdkstub").is_dir():
            return p
    return None


@pytest.mark.skipif(_stub_bot() is None, reason="athena-meeting-bot (stub SDK) not built")
def test_full_fixture_run() -> None:
    bot = _stub_bot()
    r = subprocess.run([sys.executable, str(SERVER / "scripts" / "meeting_bot_cost.py"),
                        str(bot), "--seconds", "3", "--json"],
                       capture_output=True, text=True, timeout=120)
    data = json.loads(r.stdout)
    assert r.returncode == 0 and data["ok"], data["problems"]
    assert data["status_lines"][-1] == "ended meeting_ended"
    assert data["uploads"]["dana_chunks"] >= 1 and data["uploads"]["end"] == 1
    assert data["memory"]["vmpeak_bytes"] > 0
