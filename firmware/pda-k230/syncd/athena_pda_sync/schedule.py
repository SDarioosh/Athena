"""When to sync (T5 plan §3.4, adapted for a link that stays up; pure, unit tested).

- On connect, and on `sync_now` from the UI: at once (`sync_now` also cuts a backoff short:
  the user asked).
- When an unasked STATUS shows more outbox entries than the last sync left waiting: at
  once, unless backing off.
- Otherwise every 5 min while the UI says the user is active (status bit4), every 15 min
  when not, counted from the end of the last sync.
- After a failed sync (hub unreachable, token refused, a UI error): 1, 2, 5, 10, 15, 15 …
  min after that attempt, the PC relay's backoff (`drain.BACKOFF_SECONDS`). A good sync
  resets it.
"""

from __future__ import annotations

from athena_relay.drain import BACKOFF_SECONDS

ACTIVE_SECONDS = 300
IDLE_SECONDS = 900


class SyncPolicy:
    def __init__(self, *, active: float = ACTIVE_SECONDS, idle: float = IDLE_SECONDS,
                 backoff: tuple[float, ...] = BACKOFF_SECONDS) -> None:
        self.active = active
        self.idle = idle
        self.backoff = backoff
        self.failures = 0
        self.last_end: float | None = None  # monotonic time the last sync ended
        self.now_requested = True  # sync on start/connect
        self.outbox_baseline = 0  # entries the last sync left waiting

    def connected(self) -> None:
        self.now_requested = True

    def request_now(self) -> None:
        self.now_requested = True

    def outbox(self, count: int) -> None:
        """An unasked STATUS: more entries than left waiting -> sync (if not backing off)."""
        if count > self.outbox_baseline and not self.failures:
            self.now_requested = True
        elif count < self.outbox_baseline:
            self.outbox_baseline = count

    def retry_delay(self) -> float:
        """The backoff after `failures` failed syncs in a row (0 if none)."""
        if not self.failures:
            return 0.0
        return float(self.backoff[min(self.failures - 1, len(self.backoff) - 1)])

    def due(self, user_active: bool) -> float | None:
        """The monotonic time the next sync is due (None: right away)."""
        if self.now_requested or self.last_end is None:
            return None
        if self.failures:
            return self.last_end + self.retry_delay()
        return self.last_end + (self.active if user_active else self.idle)

    def wait(self, now: float, user_active: bool) -> float:
        """Seconds to wait from `now` (0: sync now)."""
        due = self.due(user_active)
        return 0.0 if due is None else max(0.0, due - now)

    def started(self) -> None:
        self.now_requested = False

    def succeeded(self, now: float, waiting: int) -> None:
        self.failures = 0
        self.last_end = now
        self.outbox_baseline = waiting

    def failed(self, now: float, outbox: int | None = None) -> float:
        """Record a failed sync; return the delay before the next try."""
        self.failures += 1
        self.last_end = now
        if outbox is not None:
            self.outbox_baseline = outbox
        return self.retry_delay()
