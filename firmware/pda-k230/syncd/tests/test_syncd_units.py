"""The hub client (routes, old-hub fallback, proxy), the tailnet state, the sync policy and
config.json."""

from __future__ import annotations

import json
import subprocess

import _syncd_path  # noqa: F401 (the import path)
import pytest
from athena_pda_sync import config as c
from athena_pda_sync import frames as fr
from athena_pda_sync import hub as h
from athena_pda_sync import tailnet as tn
from athena_pda_sync.schedule import SyncPolicy
from athena_relay.uploader import AUTH, RETRY, UploadError
from syncd_fakes import HUB_TOKEN, FakeHub


@pytest.fixture
def hub():
    fake = FakeHub()
    yield fake
    fake.close()


# ----------------------------------------------------------------- hub client


def test_hub_api_routes(hub: FakeHub) -> None:
    api = h.HubApi(hub.url, HUB_TOKEN, timeout=3)
    api.health()
    assert api.get("/v1/action-items", {"status": "open"})["action_items"]
    api.patch("/v1/action-items/1", {"status": "done"})
    assert hub.paths() == ["/api/codex/athena/health", "/api/codex/athena/action-items",
                           "/api/codex/athena/action-items/1"]
    for _, raw, headers, _ in hub.requests:
        assert headers["Authorization"] == f"Bearer {HUB_TOKEN}"
        assert HUB_TOKEN not in raw  # never in the URL
    assert HUB_TOKEN not in repr(api) and api.old_routes is False


def test_hub_api_old_hub_falls_back_once(hub: FakeHub) -> None:
    hub.old_hub = True
    api = h.HubApi(hub.url, HUB_TOKEN, timeout=3)
    api.health()
    assert api.old_routes is True
    api.get("/v1/conversations", {"limit": 20})
    assert hub.paths() == ["/api/codex/athena/health", "/api/codex/sound/health",
                           "/api/codex/sound/conversations"]


def test_hub_api_known_new_hub_keeps_404(hub: FakeHub) -> None:
    api = h.HubApi(hub.url, HUB_TOKEN, timeout=3)
    api.health()
    with pytest.raises(UploadError) as e:
        api.get("/v1/digests/2030-01-01")  # no digest: the module's 404, not a missing route
    assert e.value.status == 404
    assert hub.paths()[-1] == "/api/codex/athena/digests/2030-01-01"
    assert len(hub.paths()) == 2


def test_hub_api_auth_and_down(hub: FakeHub) -> None:
    api = h.HubApi(hub.url, HUB_TOKEN, timeout=3)
    hub.revoked = True
    with pytest.raises(UploadError) as e:
        api.health()
    assert e.value.kind == AUTH and api.last_health_error is e.value
    hub.revoked, hub.module_down = False, True
    with pytest.raises(UploadError) as e:
        api.health()
    assert e.value.kind == RETRY and e.value.status == 502
    hub.module_down = False
    api.health()
    assert api.last_health_error is None


def test_proxy_is_used(hub: FakeHub) -> None:
    # The hub URL doesn't resolve; the request reaches the fake only through the proxy,
    # which sees the absolute URL (as Tailscale's userspace HTTP proxy does).
    clients = h.Hub.make("http://ody.tailnet.invalid:7000", HUB_TOKEN, proxy=hub.url,
                         timeout=3)
    clients.api.health()
    clients.todos.notes()
    raw = [p for _, p, _, _ in hub.requests]
    assert raw == ["http://ody.tailnet.invalid:7000/api/codex/athena/health",
                   "http://ody.tailnet.invalid:7000/api/codex/todos?format=json"]


def test_environment_proxy_ignored(hub: FakeHub, monkeypatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    h.HubApi(hub.url, HUB_TOKEN, timeout=3).health()
    assert hub.paths() == ["/api/codex/athena/health"]


def test_hub_calendar_todos_insight(hub: FakeHub) -> None:
    from datetime import datetime, timezone

    clients = h.Hub.make(hub.url, HUB_TOKEN, calendar_href="cal-home", timeout=3)
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)  # noqa: UP017
    assert [e["id"] for e in clients.calendar.list_events(start, start)] == ["u1", "u2::20261022"]
    assert clients.todos.notes()[0]["id"] == "n-groceries"
    assert clients.insight.today()["date"] == "2026-10-20"
    assert clients.calendar.calendar_href == "cal-home"


# ----------------------------------------------------------------- tailnet


def _status(backend: str, **kw) -> str:
    return json.dumps({"BackendState": backend, "AuthURL": kw.get("auth", ""),
                       "Self": {"DNSName": "athena-pda.tail0000.ts.net.",
                                "HostName": "athena-pda"}})


def test_parse_status() -> None:
    assert tn.parse_status(0, _status("Running")) == tn.TailnetState(
        "running", None, "athena-pda.tail0000.ts.net")
    assert tn.parse_status(0, _status("NeedsLogin", auth="https://login.tailscale.com/a/x1")) \
        .url == "https://login.tailscale.com/a/x1"
    assert tn.parse_status(0, _status("NeedsLogin")).url is None
    assert tn.parse_status(0, _status("NeedsMachineAuth")).state == "needs_login"
    assert tn.parse_status(0, _status("Stopped")).state == "stopped"
    assert tn.parse_status(0, _status("Starting")).state == "unknown"
    assert tn.parse_status(1, "") == tn.TailnetState("stopped")  # tailscaled not running
    assert tn.parse_status(0, "garbage") == tn.TailnetState("unknown")
    no_self = json.dumps({"BackendState": "Running", "Self": None})
    assert tn.parse_status(0, no_self) == tn.TailnetState("running")
    msg = tn.TailnetState("needs_login", "https://l/x", None).message()
    assert msg == {"t": "tailnet", "state": "needs_login", "url": "https://l/x", "name": None}


def test_tailnet_missing_timeout() -> None:
    def missing(argv, timeout):
        raise FileNotFoundError(argv[0])

    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert tn.Tailnet(run=missing).poll() == tn.TailnetState("unknown")
    assert tn.Tailnet(run=slow).poll() == tn.TailnetState("unknown")


def test_tailnet_only_reads_status() -> None:
    """syncd is no Tailscale operator: it never starts a login or changes a setting, even
    when Tailscale needs a login and has no link yet (root's athena-tailnet watch does)."""
    out = {"v": _status("NeedsLogin")}
    ran: list[list[str]] = []

    def run(argv, timeout):
        ran.append(argv)
        return 0, out["v"]

    t = tn.Tailnet(binary="/usr/bin/tailscale", run=run)
    assert t.poll() == tn.TailnetState("needs_login", None, "athena-pda.tail0000.ts.net")
    out["v"] = _status("NeedsLogin", auth="https://login.tailscale.com/a/status")
    assert t.poll().url == "https://login.tailscale.com/a/status"
    out["v"] = _status("Running")
    assert t.poll().state == "running"
    assert ran == [["/usr/bin/tailscale", "status", "--json"]] * 3


# ----------------------------------------------------------------- policy


def test_policy_intervals_and_backoff() -> None:
    p = SyncPolicy()
    assert p.wait(0, False) == 0  # on start
    p.started()
    p.succeeded(100, 0)
    assert p.wait(100, True) == 300 and p.wait(100, False) == 900
    assert p.wait(350, True) == 50
    delays = []
    for k in range(7):
        p.started()
        delays.append(p.failed(1000 + k))
    assert delays == [60, 120, 300, 600, 900, 900, 900]
    assert p.wait(1006, True) == 900
    p.request_now()  # sync_now cuts the backoff short
    assert p.wait(1006, True) == 0
    p.started()
    p.succeeded(2000, 0)
    assert p.failures == 0 and p.wait(2000, False) == 900


def test_policy_outbox() -> None:
    p = SyncPolicy()
    p.started()
    p.succeeded(0, 1)  # one action left waiting (e.g. no calendar)
    p.outbox(1)
    assert p.wait(1, False) > 0  # nothing new
    p.outbox(2)
    assert p.wait(1, False) == 0  # a new entry
    p.started()
    p.succeeded(2, 0)
    p.started()
    p.failed(3, outbox=0)
    p.outbox(1)
    assert p.wait(4, False) == 59  # backing off: the outbox doesn't override it
    p.outbox(0)
    assert p.outbox_baseline == 0


# ----------------------------------------------------------------- config


def test_config(tmp_path) -> None:
    assert c.parse({}) == c.Config()
    cfg = c.parse({"tz": "Europe/Berlin", "calendar_href": " cal-1 ",
                   "templates": "Call, Meeting,,", "proxy": "http://127.0.0.1:1055/",
                   "tailscale": False, "tailscale_login": True, "unknown": 1})
    assert cfg == c.Config("Europe/Berlin", "cal-1", ["Call", "Meeting"],
                           "http://127.0.0.1:1055", False)
    bad = c.parse({"tz": "Mars/Base", "proxy": "socks5://x:1", "templates": 3,
                   "tailscale": "yes", "calendar_href": 5})
    assert bad == c.Config()
    assert c.parse([1]) == c.Config()
    path = tmp_path / "config.json"
    f = c.ConfigFile(path)
    assert f.config == c.Config()
    path.write_text(json.dumps({"tz": "Europe/Berlin"}))
    assert f.reload().tz == "Europe/Berlin"
    path.write_text("{broken")
    assert f.reload() == c.Config()
    path.unlink()
    assert f.reload() == c.Config()


def test_paths() -> None:
    p = c.Paths.from_env({})
    assert str(p.socket) == "/run/athena-pda/glance.sock"
    assert str(p.token) == "/var/lib/athena-pda/token"
    p = c.Paths.from_env({"ATHENA_PDA_DIR": "/d", "ATHENA_PDA_RUN": "/r"})
    assert (str(p.state), str(p.config), str(p.socket)) == (
        "/d/pda.json", "/d/config.json", "/r/glance.sock")


# ----------------------------------------------------------------- LOCAL sizes


def test_fit_local_cuts_by_utf8_bytes() -> None:
    cjk = "\u6c49" * 250
    net = fr.fit_local({"t": "net", "state": "ok", "hub": cjk, "last_ok": 1, "msg": cjk})
    for k in ("hub", "msg"):
        raw = net[k].encode()
        assert len(raw) <= fr.FIELD_BYTES[k] and net[k].endswith("\u2026")
        raw.decode("utf-8")  # cut at a character
    assert len(fr.encode_local(net)) <= fr.HEADER + fr.MAX_PAYLOAD
    # a short message is untouched
    small = {"t": "net", "state": "ok", "hub": "hub.example.ts.net", "last_ok": 1, "msg": ""}
    assert fr.fit_local(small) == small
    # a login link is never cut: too long, it goes as null
    long_url = "https://login.tailscale.com/a/" + "x" * 300
    tail = fr.fit_local({"t": "tailnet", "state": "needs_login", "url": long_url, "name": cjk})
    assert tail["url"] is None and len(tail["name"].encode()) <= 127
    ok_url = "https://login.tailscale.com/a/f1"
    assert fr.fit_local({"t": "tailnet", "state": "needs_login", "url": ok_url,
                         "name": None})["url"] == ok_url
    # JSON escapes (6 B per control character) and unknown fields still fit
    odd = fr.fit_local({"t": "pair_result", "ok": False, "msg": "\x01" * 120,
                        "extra": "\x02" * 400})
    assert len(fr.encode_local(odd)) <= fr.HEADER + fr.MAX_PAYLOAD
    with pytest.raises(fr.FrameError):
        fr.encode_local({"t": "net", "hub": cjk})  # unfitted, as before


# ----------------------------------------------------------------- tz


def test_tz_offsets_follow_dst() -> None:
    from athena_pda_sync import tzdays
    from athena_relay.pda_feed import resolve_tz

    # 2026-10-20 07:05 UTC (Tuesday) in Berlin: summer time (+120) until Sunday 25 Oct
    now = 1792479900.0
    msg = tzdays.message(resolve_tz("Europe/Berlin"), now)
    assert msg["t"] == "tz" and msg["day"] == 20746  # 2026-10-20
    assert msg["offsets"] == [120] * 5 + [60] * 9  # 20..24 Oct summer, 25 Oct on winter
    assert len(fr.encode_local(fr.fit_local(msg))) < 200
    assert tzdays.message(resolve_tz("UTC"), now)["offsets"] == [0] * 14
    # just before local midnight the first day is still that day
    late = tzdays.message(resolve_tz("Europe/Berlin"), 1792533599.0)  # 23:59:59 local
    assert late["day"] == 20746
    assert tzdays.message(resolve_tz("Europe/Berlin"), 1792533600.0)["day"] == 20747
