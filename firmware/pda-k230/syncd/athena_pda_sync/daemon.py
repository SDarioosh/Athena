"""The main loop of athena-pda-syncd.

- Connect to the UI's `glance.sock`; when it isn't there or the UI drops the link (a UI
  restart), try again every `reconnect` seconds. One connection at a time.
- On connect: send `net`, `tz` and `tailnet`, then sync (`schedule.SyncPolicy` says when next).
- A sync is one `PdaSync.run` (stay=False): INFO, STATUS, TIME, hub health, outbox first,
  feed, details, `BYE`. `BYE` ends the sync, not the connection.
- Between syncs, read STATUS every 30 s (the user-active bit, and a keep-alive), and react
  to unasked STATUS frames (a new outbox entry) and LOCAL messages (`sync_now`, `pair`,
  `unpair`).
- `net` is sent whenever its content changes, `tz` likewise (checked in every turn of the
  loop, at least every 30 s: a new day changes it), `tailnet` likewise (polled every 30 s, every
  5 s while it needs a login).

Privacy: no content is logged at INFO (only states, counts and ids of nothing personal);
`athena_relay`'s loggers are kept at WARNING unless --debug (its INFO lines name items).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from athena_relay import pda_protocol as q
from athena_relay import pda_sync as s
from athena_relay.drain import LinkLost
from athena_relay.pda_feed import resolve_tz

from . import frames as fr
from . import pairing as pr
from . import tzdays
from .config import ConfigFile, Paths
from .hub import Hub
from .schedule import SyncPolicy
from .tailnet import Tailnet, TailnetState
from .transport import SocketTransport

log = logging.getLogger("athena_pda_sync")

# net states (Contract 1)
UNPAIRED = "unpaired"
OFFLINE = "offline"
SYNCING = "syncing"
OK = "ok"
AUTH_FAILED = "auth_failed"

STATUS_POLL = 30.0
TAILNET_POLL = 30.0
TAILNET_LOGIN_POLL = 5.0
RECONNECT = 2.0
DEVICE_KEY = "k230"


@dataclass(frozen=True)
class Net:
    state: str
    hub: str | None
    last_ok: int
    msg: str

    def message(self) -> dict[str, Any]:
        return {"t": "net", "state": self.state, "hub": self.hub, "last_ok": self.last_ok,
                "msg": self.msg}


HubFactory = Callable[[pr.Paired, Any], Hub]


def default_hub(paired: pr.Paired, config: Any) -> Hub:
    return Hub.make(paired.url, paired.token, calendar_href=config.calendar_href,
                    proxy=config.proxy)


class Daemon:
    def __init__(self, paths: Paths, *, tailnet: Tailnet | None = None,
                 hub_factory: HubFactory = default_hub, policy: SyncPolicy | None = None,
                 redeem: Callable[..., pr.Paired] = pr.redeem,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 status_poll: float = STATUS_POLL, reconnect: float = RECONNECT,
                 tailnet_poll: float = TAILNET_POLL,
                 tailnet_login_poll: float = TAILNET_LOGIN_POLL,
                 sync_timeout: float = 10.0) -> None:
        self.paths = paths
        self.config_file = ConfigFile(paths.config)
        self.tokens = pr.TokenStore(paths.token)
        self.state = s.PdaState(paths.state)
        self.tailnet = tailnet if tailnet is not None else Tailnet()
        self.hub_factory = hub_factory
        self.policy = policy or SyncPolicy()
        self._redeem = redeem
        self.clock = clock
        self.monotonic = monotonic
        self.status_poll = status_poll
        self.reconnect = reconnect
        self.tailnet_poll = tailnet_poll
        self.tailnet_login_poll = tailnet_login_poll
        self.sync_timeout = sync_timeout

        self.paired = self.tokens.load()
        self._hub: Hub | None = None
        self._hub_key: tuple[Any, ...] | None = None
        self.t: SocketTransport | None = None
        self.wake = asyncio.Event()
        self.syncing = False
        self.user_active = False
        self.last_ok = 0
        self.net = Net(UNPAIRED if self.paired is None else OFFLINE,
                       self.paired.host if self.paired else None, 0, "")
        self._sent_net: Net | None = None
        self._sent_tz: dict[str, Any] | None = None
        self.tailnet_state = TailnetState()
        self._sent_tailnet: TailnetState | None = None
        self.reports: list[s.Report] = []  # for tests and --once
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pairing = False

    # ------------------------------------------------------------ hub

    def hub(self) -> Hub | None:
        """The clients for the current token and config, kept while neither changes (so the
        hub's route memory survives between syncs)."""
        if self.paired is None:
            return None
        config = self.config_file.reload()
        key = (self.paired.url, self.paired.token, config.calendar_href, config.proxy)
        if key != self._hub_key:
            try:
                self._hub = self.hub_factory(self.paired, config)
            except ValueError as e:
                log.error("can't use the paired hub: %s", e)
                self._hub = None
            self._hub_key = key
        return self._hub

    # ------------------------------------------------------------ messages to the UI

    async def _send(self, message: dict[str, Any]) -> None:
        t = self.t
        if t is None:
            return
        try:
            await t.send_local(message)
        except LinkLost:
            pass
        except fr.FrameError as e:  # never ends the caller (a pairing, the loop)
            log.error("LOCAL %s not sent: %s", message.get("t"), e)

    async def set_net(self, state: str | None = None, msg: str | None = None) -> None:
        net = Net(state or self.net.state, self.paired.host if self.paired else None,
                  self.last_ok, self.net.msg if msg is None else fr.cut_utf8(msg, 120))
        self.net = net
        if net != self._sent_net and self.t is not None:
            self._sent_net = net
            log.info("net: %s%s", net.state, f" ({net.msg})" if net.msg else "")
            await self._send(net.message())

    async def set_tailnet(self, state: TailnetState) -> None:
        self.tailnet_state = state
        if state != self._sent_tailnet and self.t is not None:
            self._sent_tailnet = state
            log.info("tailnet: %s", state.state)
            await self._send(state.message())

    async def set_tz(self) -> None:
        """Send `tz` (tzdays) when it differs from what this connection was last sent."""
        try:
            msg = tzdays.message(resolve_tz(self.config_file.config.tz), self.clock())
        except ValueError:  # config.py already fell back to UTC for an unknown zone
            return
        if msg != self._sent_tz and self.t is not None:
            self._sent_tz = msg
            await self._send(msg)

    # ------------------------------------------------------------ LOCAL from the UI

    def on_local(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "sync_now":
            log.info("the UI asks for a sync")
            self.policy.request_now()
            self.wake.set()
        elif kind == "pair":
            self._spawn(self.pair(msg))
        elif kind == "unpair":
            self._spawn(self.unpair())
        else:
            log.debug("LOCAL %r ignored", kind)

    def on_status(self, raw: bytes) -> None:
        try:
            st = q.Status.decode(raw)
        except q.ProtocolError:
            return
        self.user_active = st.user_active
        if not self.syncing:
            self.policy.outbox(st.outbox)
        self.wake.set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def pair(self, msg: dict[str, Any]) -> None:
        url, code = msg.get("url"), msg.get("code")
        if self._pairing:
            await self._send({"t": "pair_result", "ok": False,
                              "msg": "A pairing is already running."})
            return
        self._pairing = True
        try:
            if isinstance(url, str) and url.strip().startswith(pr.PREFIX) and not code:
                url, code = pr.parse_link(url)  # the raw QR text in `url`
            elif isinstance(msg.get("link"), str) and not url:
                url, code = pr.parse_link(msg["link"])
            if not isinstance(url, str) or not isinstance(code, str):
                raise pr.PairError("Type the Odysseus address and the pairing code.")
            proxy = self.config_file.reload().proxy
            paired = await asyncio.to_thread(self._redeem, url, code, proxy=proxy)
            await asyncio.to_thread(self.tokens.save, paired)
        except pr.PairError as e:
            log.warning("pairing failed: %s", e)
            await self._send({"t": "pair_result", "ok": False, "msg": str(e)})
            return
        except OSError as e:
            log.error("can't save the token: %s", e.strerror or e)
            await self._send({"t": "pair_result", "ok": False,
                              "msg": "Paired, but the token can't be saved on this device."})
            return
        finally:
            self._pairing = False
        log.info("paired with %s", paired.host)
        self.paired = paired
        self.last_ok = 0
        self.policy = SyncPolicy(active=self.policy.active, idle=self.policy.idle,
                                 backoff=self.policy.backoff)
        await self._send({"t": "pair_result", "ok": True,
                          "msg": f"Paired with {paired.host}."})
        await self.set_net(OFFLINE, "not synced yet")
        self.wake.set()

    async def unpair(self) -> None:
        removed = await asyncio.to_thread(self.tokens.delete)
        log.info("unpaired%s", "" if removed else " (there was no token)")
        self.paired = None
        self._hub, self._hub_key = None, None
        self.last_ok = 0
        await self.set_net(UNPAIRED, "")
        self.wake.set()

    # ------------------------------------------------------------ syncing

    async def sync_once(self, t: SocketTransport) -> s.Report | None:
        """One sync over `t`; updates `net` and the policy. None if not paired."""
        hub = self.hub()
        if hub is None:
            if self.paired is None:
                await self.set_net(UNPAIRED, "")
            else:
                self.policy.started()
                self.policy.failed(self.monotonic())
                await self.set_net(AUTH_FAILED, "The saved hub address is not usable: "
                                                "pair again.")
            return None
        paired = self.paired
        config = self.config_file.config
        self.policy.started()
        self.syncing = True
        await self.set_net(SYNCING)
        sync = s.PdaSync(hub.api, tz_name=config.tz, calendar=hub.calendar,
                         templates=config.templates or None, todos=hub.todos,
                         insight=hub.insight, state=self.state, device_key=DEVICE_KEY,
                         timeout=self.sync_timeout)
        try:
            report = await sync.run(t)
        finally:
            self.syncing = False
        self.reports.append(report)
        if self.paired is not paired:  # unpaired or paired again during the sync
            return report
        await self._after(t, hub, report)
        return report

    async def _after(self, t: SocketTransport, hub: Hub, report: s.Report) -> None:
        now = self.monotonic()
        outcome = report.outcome
        health = hub.api.last_health_error  # for the wording of a SERVER_RETRY only
        if outcome == s.DONE:
            self.last_ok = int(self.clock())
            self.policy.succeeded(now, len(report.waiting))
            n = len(report.waiting)
            await self.set_net(OK, f"{n} change{'s' if n != 1 else ''} waiting" if n else "")
            # entries queued while the sync ran: sync again at once
            with contextlib.suppress(LinkLost, TimeoutError, asyncio.TimeoutError,  # noqa: UP041
                                     q.ProtocolError):
                st = q.Status.decode(await t.read_status())
                self.user_active = st.user_active
                if st.outbox > len(report.waiting):
                    self.policy.request_now()
            return
        if outcome == s.LINK_LOST and t.closed.is_set():
            # the UI went away; the next connection is told this and syncs at once
            await self.set_net(OK if self.last_ok else OFFLINE)
            return
        outbox = report.status.outbox if report.status else None
        delay = self.policy.failed(now, outbox)
        if outcome == s.AUTH_FAILED:
            await self.set_net(AUTH_FAILED, "The hub refused this device's token: pair again.")
        elif outcome == s.SERVER_RETRY:
            if health is None:
                msg = "Hub or Athena error"  # health was fine, a later call failed
            elif health.status is None:
                msg = "Hub not reachable"
            else:
                msg = f"Hub answered HTTP {health.status}"  # 502: Athena down behind it
            await self.set_net(OFFLINE, f"{msg}; retrying in {_minutes(delay)}.")
        elif outcome == s.INCOMPATIBLE:
            await self.set_net(OFFLINE, "The screen speaks another glance protocol: update.")
        else:  # DEVICE_RETRY, or LINK_LOST by a timeout: the UI's side, not the hub's
            state = OK if self.last_ok else OFFLINE
            await self.set_net(state, f"Sync error on this device; retrying in "
                                      f"{_minutes(delay)}.")
        log.warning("sync %s; next try in %s", outcome, _minutes(delay))

    # ------------------------------------------------------------ connection

    async def session(self, t: SocketTransport) -> None:
        """Serve one UI connection until it drops."""
        self.t = t
        self._sent_net = self._sent_tailnet = self._sent_tz = None
        self.policy.connected()
        tail: asyncio.Task[None] | None = None
        try:
            await self.set_net()
            await self.set_tz()
            tail = asyncio.create_task(self._tailnet_loop(t))  # polls, then sends tailnet
            while not t.closed.is_set():
                await self.set_tz()  # a new day (or tz): the next 14 days' offsets
                if self.paired is not None:
                    wait = self.policy.wait(self.monotonic(), self.user_active)
                    if wait <= 0:
                        await self.sync_once(t)
                        continue
                else:
                    wait = float("inf")
                if await self._idle(t, min(wait, self.status_poll)):
                    await self._poll_status(t)
        finally:
            if tail is not None:
                tail.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tail
            self.t = None

    async def _idle(self, t: SocketTransport, timeout: float) -> bool:
        """Wait for a wake-up, the link to drop, or `timeout`. True on the timeout."""
        self.wake.clear()
        waits = [asyncio.create_task(self.wake.wait()), asyncio.create_task(t.closed.wait())]
        done, pending = await asyncio.wait(waits, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        return not done

    async def _poll_status(self, t: SocketTransport) -> None:
        with contextlib.suppress(LinkLost, TimeoutError, asyncio.TimeoutError,  # noqa: UP041
                                 q.ProtocolError):
            st = q.Status.decode(await t.read_status())
            self.user_active = st.user_active

    async def _tailnet_loop(self, t: SocketTransport) -> None:
        while not t.closed.is_set():
            if self.config_file.config.tailscale:
                state = await asyncio.to_thread(self.tailnet.poll)
            else:
                state = TailnetState()
            await self.set_tailnet(state)
            pause = self.tailnet_login_poll if state.state == "needs_login" \
                else self.tailnet_poll
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                await asyncio.wait_for(t.closed.wait(), pause)

    async def run_forever(self, *, once: bool = False) -> int:
        """Connect, serve, reconnect. With `once`: one sync, then exit (0 ok, 1 changes
        waiting, 2 retry later, 3 unpaired or token refused)."""
        path = self.paths.socket
        logged = False
        while True:
            try:
                t = await SocketTransport.connect(path, on_local=self.on_local,
                                                  on_status=self.on_status)
            except OSError as e:
                if not logged:
                    log.info("waiting for the UI at %s (%s)", path, e.strerror or e)
                    logged = True
                await asyncio.sleep(self.reconnect)
                continue
            logged = False
            log.info("connected to the UI")
            try:
                if once:
                    return await self._once(t)
                await self.session(t)
            finally:
                await t.close()
            log.info("the UI closed the link (%s); reconnecting", t.why or "closed")
            await asyncio.sleep(self.reconnect)

    async def _once(self, t: SocketTransport) -> int:
        self.t = t
        await self.set_net()
        report = await self.sync_once(t)
        if report is None or self.net.state == AUTH_FAILED:
            return 3
        if self.net.state != OK:
            return 2
        return 1 if report.waiting else 0


def _minutes(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} s"
    return f"{int(round(seconds / 60))} min"
