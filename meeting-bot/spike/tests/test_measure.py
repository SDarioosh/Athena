"""measure.sh against a stand-in bot (no Zoom SDK needed)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SPIKE = Path(__file__).resolve().parents[1]
MEASURE = SPIKE / "measure.sh"
FAKE = SPIKE / "tests" / "fake_bot.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or not Path("/proc").is_dir(),
                                reason="needs bash and /proc")


def _row(results: Path) -> list[str]:
    lines = results.read_text().splitlines()
    assert lines[0].startswith("| Run | Duration (min) | CPU avg % |")
    return [c.strip() for c in lines[-1].strip("|").split("|")]


def _kill_leftovers() -> None:
    subprocess.run(["pkill", "-f", "time[.]sleep[(]60"], check=False)
    for f in Path("/tmp").glob("athena-spike-test.*"):
        f.unlink(missing_ok=True)


def test_clean_run_reports_timing_and_nothing_left(tmp_path):
    results = tmp_path / "m.md"
    r = subprocess.run([str(MEASURE), "-o", str(results), "-l", "clean", "-i", "1", "--",
                        str(FAKE)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "FAKE_SECONDS": "3"})
    assert r.returncode == 0, r.stdout + r.stderr
    run, duration, cpu_avg, cpu_peak, rss, to_recording, left = _row(results)
    assert run == "clean"
    assert float(duration) > 0
    assert float(cpu_peak) >= float(cpu_avg) > 5  # the stand-in spins one core
    assert int(rss) >= 1
    assert 0.5 <= float(to_recording) <= 4
    assert left == "none"
    summary = next(tmp_path.glob("clean-*/summary.md")).read_text()
    assert "last state: ended duration" in summary


def test_leftover_process_socket_and_temp_file_fail_the_run(tmp_path):
    results = tmp_path / "m.md"
    try:
        r = subprocess.run([str(MEASURE), "-o", str(results), "-l", "leak", "-i", "1", "--",
                            str(FAKE)], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "FAKE_SECONDS": "1", "FAKE_LEAVE_BEHIND": "1"})
    finally:
        _kill_leftovers()
    assert r.returncode == 1, r.stdout + r.stderr
    left = _row(results)[-1]
    assert "1 process(es)" in left
    assert "listening socket(s)" in left
    assert "temp file(s)" in left


def test_attach_to_running_bot(tmp_path):
    results = tmp_path / "m.md"
    log = tmp_path / "bot.log"
    with log.open("w") as out:
        bot = subprocess.Popen([str(FAKE)], stdout=out, stderr=subprocess.STDOUT,
                               env={**os.environ, "FAKE_SECONDS": "3"},
                               start_new_session=True)
    try:
        time.sleep(0.3)
        r = subprocess.run([str(MEASURE), "-o", str(results), "-l", "attach", "-i", "1",
                            "-p", str(bot.pid), "-L", str(log)],
                           capture_output=True, text=True, timeout=60)
    finally:
        if bot.poll() is None:
            os.killpg(bot.pid, signal.SIGTERM)
        bot.wait()
    assert r.returncode == 0, r.stdout + r.stderr
    row = _row(results)
    assert row[0] == "attach"
    assert row[-1] == "none"
    assert float(row[5]) <= 4  # timed from the bot's own start, not from attaching


def test_usage_errors():
    assert subprocess.run([str(MEASURE)], capture_output=True).returncode == 2
    assert subprocess.run([str(MEASURE), "-p", "1"], capture_output=True).returncode == 2
