"""athena-pda-syncd end to end: the daemon against a fake UI on a real Unix socket (the PC
relay's fake T5 behind it) and a fake Odysseus over HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import stat
from collections.abc import Awaitable, Callable

import _syncd_path  # noqa: F401 (the import path)
from athena_pda_sync import pairing as pr
from athena_pda_sync.config import Paths
from athena_pda_sync.daemon import Daemon
from athena_pda_sync.schedule import SyncPolicy
from athena_pda_sync.tailnet import TailnetState
from athena_relay import pda_protocol as q
from syncd_fakes import CODE, HUB_TOKEN, FakeHub, FakeTailnet, FakeUi, short_tmp


class Rig:
    def __init__(self) -> None:
        self.tmp = short_tmp()
        self.paths = Paths(self.tmp / "data", self.tmp / "run")
        self.paths.run.mkdir(parents=True)
        self.ui = FakeUi(self.paths.socket)
        self.hub = FakeHub()
        self.tailnet = FakeTailnet()
        self.daemon: Daemon | None = None
        self.task: asyncio.Task | None = None

    def pair(self, url: str | None = None) -> None:
        pr.TokenStore(self.paths.token).save(pr.Paired(url or self.hub.url, HUB_TOKEN,
                                                       "Phone relay"))

    def config(self, **kw) -> None:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(json.dumps(kw))

    async def start(self, **kw) -> Daemon:
        await self.ui.start()
        self.daemon = Daemon(self.paths, tailnet=self.tailnet,
                             policy=kw.pop("policy", SyncPolicy()), status_poll=0.1,
                             reconnect=0.05, tailnet_poll=0.1, tailnet_login_poll=0.05,
                             sync_timeout=3.0, **kw)
        self.task = asyncio.create_task(self.daemon.run_forever())
        return self.daemon

    async def reports(self, n: int, timeout: float = 15.0) -> None:
        await self.ui.wait_for(lambda: len(self.daemon.reports) >= n, timeout)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.ui.stop()
        self.hub.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def run(test: Callable[[Rig], Awaitable[None]]) -> None:
    async def go() -> None:
        rig = Rig()
        try:
            await asyncio.wait_for(test(rig), 60)
        finally:
            await rig.close()

    asyncio.run(go())


def states(ui: FakeUi) -> list[str]:
    return [m["state"] for m in ui.of("net")]


# ----------------------------------------------------------------- a full sync


def test_full_sync() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.config(tz="Europe/Berlin", calendar_href="cal-home", templates=["Call", "Gym"])
        t5 = rig.ui.t5
        item = t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
        fetch = t5.queue_action({"t": "fetch", "ref": "conv:c_new"})
        cal = t5.queue_action({"t": "cal_add", "title": "Call Tom",
                               "start": "2026-11-14T09:00:00+01:00",
                               "end": "2026-11-14T09:30:00+01:00"})
        todo = t5.queue_action({"t": "todo_toggle", "note": "n-groceries", "i": 0,
                                "text": "Milk", "done": True})
        await rig.start()
        await rig.ui.wait_local("net", state="ok")
        await rig.reports(1)
        report = rig.daemon.reports[0]
        assert report.outcome == "done", report.message
        assert dict(t5.acks) == {item: 0, cal: 0, todo: 0, fetch: 0}
        assert not t5.outbox
        # outbox first, then the feed, then the detail (acked after its PUT_OK)
        ops = t5.opcodes()
        assert ops.index(q.OP_OB_LIST) < ops.index(q.OP_PUT_BEGIN)
        assert t5.puts == [(q.KIND_FEED, t5.feed_version), (q.KIND_DETAIL, fetch)]
        assert t5.acks[-1] == (fetch, 0)
        assert ops[-1] == q.OP_BYE
        assert t5.tz_offset_min == 60  # Europe/Berlin in November
        feed = t5.feed_json()
        assert feed["tz"] == "Europe/Berlin" and feed["templates"] == ["Call", "Gym"]
        assert [c["id"] for c in feed["conversations"]] == ["c_new", "c_old"]
        assert {e["id"] for e in feed["calendar"]} >= {"u1", "u2::20261022"}
        assert feed["todos"][0]["items"][0] == {"i": 0, "text": "Milk", "done": True}
        assert feed["insight"]["date"] == "2026-10-20"
        detail = json.loads(t5.details[fetch])
        assert detail["ref"] == "conv:c_new" and detail["title"] == "Standup"
        # all through the hub's token routes
        paths = rig.hub.paths()
        assert paths[0] == "/api/codex/athena/health"
        assert paths.index("/api/codex/athena/action-items/1") < \
            paths.index("/api/codex/athena/conversations")
        assert "/api/codex/calendar/events" in paths and "/api/codex/todos" in paths
        assert rig.hub.ody.bodies()[0]["calendar_href"] == "cal-home"
        for _, raw, headers, _ in rig.hub.requests:
            assert headers["Authorization"] == f"Bearer {HUB_TOKEN}"
            assert HUB_TOKEN not in raw
        # the UI was told
        net = rig.ui.of("net")
        assert states(rig.ui)[:3] == ["offline", "syncing", "ok"]
        assert net[-1]["hub"] == "127.0.0.1" and net[-1]["last_ok"] > 0
        assert net[-1]["msg"] == ""
        assert rig.ui.of("tailnet")[0] == {"t": "tailnet", "state": "running", "url": None,
                                           "name": "athena-pda.tail0000.ts.net"}
        # the state file remembers the pushed feed and the created event
        saved = json.loads(rig.paths.state.read_text())
        assert saved["feeds"]["k230"]["version"] == t5.feed_version
        assert len(saved["cal_created"]) == 1

    run(t)


def test_unchanged_feed_not_sent_again() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        await rig.start()
        await rig.reports(1)
        puts = len(rig.ui.t5.puts)
        await rig.ui.send_local({"t": "sync_now"})
        await rig.reports(2)
        assert rig.daemon.reports[1].feed_unchanged == 1
        assert len(rig.ui.t5.puts) == puts

    run(t)


# ----------------------------------------------------------------- failures


def test_server_error_no_ack_then_backoff() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        t5 = rig.ui.t5
        aid = t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
        rig.hub.athena.fail["/v1/action-items/1"] = [503]
        await rig.start()
        await rig.reports(1)
        assert rig.daemon.reports[0].outcome == "server_retry"
        assert t5.acks == [] and aid in t5.outbox  # delivery rule: no ack on 5xx
        assert t5.feed is None  # the sync stopped, no feed
        net = await rig.ui.wait_local("net", state="offline", msg="Hub or Athena error; "
                                                                  "retrying in 1 min.")
        assert net["last_ok"] == 0
        assert rig.daemon.policy.failures == 1
        await asyncio.sleep(0.5)
        assert len(rig.daemon.reports) == 1  # backing off, not retrying at once
        await rig.ui.send_local({"t": "sync_now"})  # the user asks: try now
        await rig.reports(2)
        assert rig.daemon.reports[1].outcome == "done"
        assert t5.acks == [(aid, 0)]
        assert rig.daemon.policy.failures == 0

    run(t)


def test_hub_unreachable_offline() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.hub.drop = True
        await rig.start()
        await rig.reports(1)
        await rig.ui.wait_local("net", state="offline", msg="Hub not reachable; retrying in "
                                                            "1 min.")
        await rig.ui.send_local({"t": "sync_now"})
        await rig.reports(2)
        await rig.ui.wait_local("net", state="offline", msg="Hub not reachable; retrying in "
                                                            "2 min.")
        assert rig.daemon.policy.retry_delay() == 120

    run(t)


def test_auth_failed() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.hub.revoked = True
        await rig.start()
        net = await rig.ui.wait_local("net", state="auth_failed")
        assert "pair again" in net["msg"]
        assert rig.ui.t5.feed is None

    run(t)


def test_auth_failed_during_feed() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.hub.ody.fail = [401]  # health is fine, the calendar refuses the token
        await rig.start()
        await rig.ui.wait_local("net", state="auth_failed")
        assert rig.daemon.reports[0].outcome == "auth_failed"

    run(t)


# ----------------------------------------------------------------- triggers


def test_outbox_status_triggers_sync() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        await rig.start()
        await rig.reports(1)
        aid = rig.ui.t5.queue_action({"t": "digest_refresh", "date": "2026-10-19"})
        await rig.ui.notify_status()  # the UI's unasked STATUS: outbox 1
        await rig.reports(2)
        assert rig.ui.t5.acks[-1] == (aid, 0)

    run(t)


def test_interval_sync_while_active() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.ui.t5.flags |= q.ST_USER_ACTIVE
        await rig.start(policy=SyncPolicy(active=0.3, idle=60))
        await rig.reports(3)  # the first, then every 0.3 s while active
        rig.ui.t5.flags &= ~q.ST_USER_ACTIVE
        await asyncio.sleep(0.3)  # the next status read sees it inactive
        n = len(rig.daemon.reports)
        await asyncio.sleep(1.0)
        assert len(rig.daemon.reports) <= n + 1

    run(t)


def test_reconnect_after_ui_restart() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        t5 = rig.ui.t5
        aid = t5.queue_action({"t": "item_status", "id": 2, "status": "done"})
        t5.drop_on_ack = {aid}  # the link drops instead of OB_ACKED
        await rig.start()
        await rig.ui.wait_for(lambda: rig.ui.connections >= 2)  # reconnected
        await rig.ui.wait_for(lambda: (aid, 0) in t5.acks)  # sent again, acked
        await rig.ui.wait_local("net", state="ok")
        # the UI process restarts: the socket goes away and comes back
        await rig.ui.stop()
        n = len(rig.daemon.reports)
        await asyncio.sleep(0.2)
        await rig.ui.start()
        await rig.ui.wait_for(lambda: rig.ui.connections >= 3)
        await rig.reports(n + 1)
        assert rig.daemon.reports[-1].outcome == "done"
        # each connection starts with net and tailnet
        assert rig.ui.frames  # frames seen on the last connection

    run(t)


# ----------------------------------------------------------------- pairing


def test_pair_and_unpair() -> None:
    async def t(rig: Rig) -> None:
        await rig.start()
        first = await rig.ui.wait_local("net")
        assert first == {"t": "net", "state": "unpaired", "hub": None, "last_ok": 0, "msg": ""}
        await asyncio.sleep(0.3)
        assert rig.daemon.reports == []  # nothing to sync
        await rig.ui.send_local({"t": "pair", "url": rig.hub.url, "code": "bad"})
        res = await rig.ui.wait_local("pair_result")
        assert res["ok"] is False and "not a pairing code" in res["msg"]
        await rig.ui.send_local({"t": "pair", "url": rig.hub.url, "code": CODE})
        res = await rig.ui.wait_local("pair_result", 2)
        assert res == {"t": "pair_result", "ok": True, "msg": "Paired with 127.0.0.1."}
        st = rig.paths.token.stat()
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert json.loads(rig.paths.token.read_text())["token"] == HUB_TOKEN
        await rig.ui.wait_local("net", state="ok")  # synced right after pairing
        # the code is single use
        await rig.ui.send_local({"t": "pair", "url": rig.hub.url, "code": CODE})
        res = await rig.ui.wait_local("pair_result", 3)
        assert res["ok"] is False and "not valid any more" in res["msg"]
        assert rig.paths.token.exists()  # a failed pairing keeps the old token
        await rig.ui.send_local({"t": "unpair"})
        await rig.ui.wait_local("net", 1, state="unpaired", hub=None, last_ok=0, msg="")
        await rig.ui.wait_for(lambda: states(rig.ui)[-1] == "unpaired")
        assert not rig.paths.token.exists()
        n = len(rig.daemon.reports)
        await rig.ui.send_local({"t": "sync_now"})
        await asyncio.sleep(0.3)
        assert len(rig.daemon.reports) == n

    run(t)


def test_pair_with_raw_qr_text_and_expired_code() -> None:
    async def t(rig: Rig) -> None:
        rig.hub.codes["Expired_code_k230"] = 0.0
        await rig.start()
        await rig.ui.wait_local("net")
        link = f"odysseus-pair:v1?url={rig.hub.url.replace(':', '%3A').replace('/', '%2F')}"
        await rig.ui.send_local({"t": "pair", "url": link + "&code=Expired_code_k230"})
        res = await rig.ui.wait_local("pair_result")
        assert res["ok"] is False and "not valid any more" in res["msg"]
        assert not rig.paths.token.exists()
        await rig.ui.send_local({"t": "pair", "url": link + "&code=" + CODE})
        res = await rig.ui.wait_local("pair_result", 2)
        assert res["ok"] is True
        await rig.ui.wait_local("net", state="ok")

    run(t)


# ----------------------------------------------------------------- tailnet, proxy, once


def test_tailnet_messages() -> None:
    async def t(rig: Rig) -> None:
        rig.tailnet.state = TailnetState("needs_login", "https://login.tailscale.com/a/f1", None)
        await rig.start()
        msg = await rig.ui.wait_local("tailnet")
        assert msg == {"t": "tailnet", "state": "needs_login",
                       "url": "https://login.tailscale.com/a/f1", "name": None}
        await asyncio.sleep(0.2)
        assert len(rig.ui.of("tailnet")) == 1  # only on change
        rig.tailnet.state = TailnetState("running", None, "athena-pda.tail0000.ts.net")
        msg = await rig.ui.wait_local("tailnet", state="running")
        assert msg["url"] is None and msg["name"] == "athena-pda.tail0000.ts.net"

    run(t)


def test_tailnet_off_in_config() -> None:
    async def t(rig: Rig) -> None:
        rig.config(tailscale=False)
        await rig.start()
        msg = await rig.ui.wait_local("tailnet")
        assert msg["state"] == "unknown" and rig.tailnet.polls == 0

    run(t)


def test_proxy_from_config() -> None:
    async def t(rig: Rig) -> None:
        rig.pair("http://ody.tailnet.invalid:7000")  # reachable only through the proxy
        rig.config(proxy=rig.hub.url)
        await rig.start()
        net = await rig.ui.wait_local("net", state="ok")
        assert net["hub"] == "ody.tailnet.invalid"
        assert all(raw.startswith("http://ody.tailnet.invalid:7000/")
                   for _, raw, _, _ in rig.hub.requests)

    run(t)


def test_once() -> None:
    async def go() -> None:
        rig = Rig()
        try:
            rig.pair()
            await rig.ui.start()
            d = Daemon(rig.paths, tailnet=rig.tailnet, reconnect=0.05, sync_timeout=3.0)
            assert await asyncio.wait_for(d.run_forever(once=True), 30) == 0
            rig.ui.t5.queue_action({"t": "cal_delete", "id": "u2::20261022"})
            rig.hub.ody.fail = [503]
            d2 = Daemon(rig.paths, tailnet=rig.tailnet, reconnect=0.05, sync_timeout=3.0)
            assert await asyncio.wait_for(d2.run_forever(once=True), 30) == 2
            pr.TokenStore(rig.paths.token).delete()
            d3 = Daemon(rig.paths, tailnet=rig.tailnet, reconnect=0.05, sync_timeout=3.0)
            assert await asyncio.wait_for(d3.run_forever(once=True), 30) == 3
        finally:
            await rig.close()

    asyncio.run(go())


# ----------------------------------------------------------------- privacy


def test_logs_hold_no_token_and_no_content(caplog) -> None:
    import logging

    from athena_pda_sync.__main__ import setup_logging

    relay_log = logging.getLogger("athena_relay")
    before = relay_log.level
    setup_logging(False)
    caplog.set_level(logging.INFO)
    relay_log.setLevel(logging.WARNING)  # what setup_logging does (caplog resets levels)

    async def t(rig: Rig) -> None:
        rig.ui.t5.queue_action({"t": "todo_toggle", "note": "n-groceries", "i": 0,
                                "text": "Milk", "done": True})
        rig.ui.t5.queue_action({"t": "fetch", "ref": "conv:c_new"})
        await rig.start()
        await rig.ui.wait_local("net")
        await rig.ui.send_local({"t": "pair", "url": rig.hub.url, "code": CODE})
        await rig.ui.wait_local("net", state="ok")

    try:
        run(t)
    finally:
        relay_log.setLevel(before)
    text = caplog.text
    assert "paired with 127.0.0.1" in text and "net: ok" in text
    for secret in (HUB_TOKEN, CODE, "Milk", "Standup", "Groceries", "c_new"):
        assert secret not in text, secret


def test_module_down_behind_the_hub() -> None:
    async def t(rig: Rig) -> None:
        rig.pair()
        rig.hub.module_down = True
        await rig.start()
        await rig.ui.wait_local("net", state="offline",
                                msg="Hub answered HTTP 502; retrying in 1 min.")

    run(t)


# ----------------------------------------------------------------- long LOCAL messages


def test_a_long_non_ascii_hub_host_fits_the_local_frames() -> None:
    """250 CJK characters are 750 B of UTF-8: `net` and `pair_result` would be over the
    512 B LOCAL limit, which ended the pairing task (and the loop) with a FrameError."""
    host = "汉" * 250 + ".example"

    async def t(rig: Rig) -> None:
        rig.pair(f"http://{host}:7000")
        await rig.start()
        net = await rig.ui.wait_local("net")
        assert net["hub"].startswith("汉") and net["hub"].endswith("…")
        assert len(net["hub"].encode()) <= 127
        # what the UI can send (its pair frame is at most 512 B too): 140 of them, 420 B;
        # syncd's answer names the host
        short = "\u6c49" * 140 + ".example"
        await rig.ui.send_local({"t": "pair", "url": f"http://{short}:7000", "code": CODE})
        res = await rig.ui.wait_local("pair_result", timeout=30)
        assert res["ok"] is False and len(res["msg"].encode()) <= 127
        assert not rig.task.done()  # the daemon is still running
        await rig.ui.send_local({"t": "pair", "url": rig.hub.url, "code": CODE})
        res = await rig.ui.wait_local("pair_result", 2, timeout=30)
        assert res["ok"] is True
        await rig.ui.wait_local("net", state="ok")

    run(t)


def test_tz_on_connect_and_when_the_day_turns() -> None:
    now = [1792479900.0]  # 2026-10-20 07:05 UTC, Berlin on summer time until the 25th

    async def t(rig: Rig) -> None:
        rig.config(tz="Europe/Berlin")
        await rig.start(clock=lambda: now[0])
        first = await rig.ui.wait_local("tz")
        assert first == {"t": "tz", "day": 20746, "offsets": [120] * 5 + [60] * 9}
        await asyncio.sleep(0.3)
        assert len(rig.ui.of("tz")) == 1  # unchanged: not sent again
        now[0] += 86400
        nxt = await rig.ui.wait_local("tz", 2)
        assert nxt["day"] == 20747 and nxt["offsets"][:4] == [120] * 4

    run(t)
