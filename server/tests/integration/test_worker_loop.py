"""The background worker: one failing step doesn't skip the others; stop() waits."""

import logging
import threading
import time
from types import SimpleNamespace

from athena.pipeline import Worker


class _Store:
    def requeue_interrupted(self) -> int:
        return 0

    def sessions_with_status(self, status: str) -> list:
        return []


class _Pipeline:
    """Records which steps ran; the steps named in ``fail`` raise."""

    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.fail = set(fail)
        self.ran: list[str] = []
        self.store = _Store()
        self.summarizer = None  # no digest step work
        self.settings = SimpleNamespace(transcript_retention_days=0)

    def _step(self, name: str) -> None:
        self.ran.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} broke")

    def finalize_idle(self, now=None) -> None:
        self._step("finalize_idle")

    def process_pending(self, now=None) -> None:
        self._step("process_pending")

    def transcribe_live(self) -> None:
        self._step("transcribe_live")

    def rewrite_forgotten(self) -> None:
        self._step("rewrite_forgotten")


def test_failing_step_does_not_skip_the_rest(caplog) -> None:
    p = _Pipeline(fail={"finalize_idle", "process_pending"})
    worker = Worker(p)  # type: ignore[arg-type]
    digests: list[int] = []
    prunes: list[int] = []
    worker._maybe_digest = lambda: digests.append(1)  # type: ignore[method-assign]
    worker._maybe_prune = lambda: prunes.append(1)  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="athena.pipeline"):
        worker.tick(now=100.0)
    assert p.ran == ["finalize_idle", "process_pending", "transcribe_live",
                     "rewrite_forgotten"]
    assert digests == [1] and prunes == [1]
    messages = [r.getMessage() for r in caplog.records]
    assert "worker step finalize_idle failed" in messages
    assert "worker step process_pending failed" in messages


def test_failing_digest_still_prunes() -> None:
    worker = Worker(_Pipeline())  # type: ignore[arg-type]
    prunes: list[int] = []

    def broken() -> None:
        raise ValueError("digest broke")

    worker._maybe_digest = broken  # type: ignore[method-assign]
    worker._maybe_prune = lambda: prunes.append(1)  # type: ignore[method-assign]
    worker.tick()
    assert prunes == [1]


def test_stop_joins_the_thread() -> None:
    p = _Pipeline()
    worker = Worker(p, interval=0.01)  # type: ignore[arg-type]
    worker.start()
    deadline = time.monotonic() + 5
    while not p.ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert p.ran, "worker never ticked"
    worker.stop(timeout=5)
    assert not worker.is_alive()


def test_stop_waits_for_a_busy_pass_up_to_the_timeout() -> None:
    p = _Pipeline()
    started, release = threading.Event(), threading.Event()

    def slow() -> None:
        started.set()
        release.wait(5)

    p.rewrite_forgotten = slow  # type: ignore[method-assign]
    worker = Worker(p, interval=0.01)  # type: ignore[arg-type]
    worker.start()
    assert started.wait(5)
    t0 = time.monotonic()
    worker.stop(timeout=0.2)  # gives up after the timeout, doesn't hang
    assert 0.15 <= time.monotonic() - t0 < 2
    assert worker.is_alive()
    release.set()
    worker.join(5)
    assert not worker.is_alive()


def test_stop_before_start_is_harmless() -> None:
    worker = Worker(_Pipeline())  # type: ignore[arg-type]
    worker.stop()
    assert not worker.is_alive()
