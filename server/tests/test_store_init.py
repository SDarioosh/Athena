"""The API server and the MCP server open (and migrate) the same database at once."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from convo_server.store import Store


def _open(path: str, barrier, results) -> None:
    barrier.wait()
    try:
        Store(path).close()
        results.put("ok")
    except Exception as e:  # noqa: BLE001 - reported to the parent
        results.put(f"{type(e).__name__}: {e}")


def test_concurrent_open_of_a_fresh_database(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    outcomes = []
    for i in range(5):
        path = str(tmp_path / f"convo{i}.db")
        barrier, results = ctx.Barrier(4), ctx.Queue()
        procs = [ctx.Process(target=_open, args=(path, barrier, results)) for _ in range(4)]
        for p in procs:
            p.start()
        outcomes += [results.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=60)
    assert outcomes == ["ok"] * len(outcomes)
    assert Store(str(tmp_path / "convo0.db"))._conn.execute(
        "PRAGMA user_version").fetchone()[0] == 8
