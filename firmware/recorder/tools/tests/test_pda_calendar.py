"""The Odysseus calendar client (docs/t5-pda-plan.md §6) against a fake Odysseus, and the
feed's optional `templates`."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from barvis_relay import pda_actions as a
from barvis_relay import pda_feed as f
from barvis_relay import pda_sync as s
from barvis_relay.pda_calendar import OdysseusCalendar
from barvis_relay.pda_insight import OdysseusInsight
from barvis_relay.pda_todos import OdysseusTodos
from ody_fake import ODY_TOKEN, FakeOdysseus
from pda_fakes import NOW, TOKEN, FakeApiServer, FakeT5

UTC = timezone.utc  # noqa: UP017


@pytest.fixture
def ody():
    o = FakeOdysseus()
    yield o
    o.close()


@pytest.fixture
def api_server():
    srv = FakeApiServer()
    yield srv
    srv.close()


def cal(o: FakeOdysseus, token: str = ODY_TOKEN, href: str | None = "cal-caldav"):
    return OdysseusCalendar(o.url, token, calendar_href=href, timeout=3.0)


def server_api(srv: FakeApiServer) -> f.HttpApi:
    return f.HttpApi(srv.url, TOKEN, timeout=3.0)


def test_list_maps_events(ody) -> None:
    start = datetime(2026, 10, 20, tzinfo=UTC)
    events = cal(ody).list_events(start, start + timedelta(days=8))
    assert events[0] == {"id": "u1", "title": "Dentist follow-up",
                         "start": "2026-10-21T09:00:00Z", "end": "2026-10-21T09:30:00Z",
                         "where": "Dr. Patel", "all_day": False}
    assert events[1]["all_day"] and events[1]["where"] is None
    method, path, headers, _ = ody.requests[0]
    assert method == "GET" and "start=2026-10-20T00%3A00%3A00%2B00%3A00" in path
    assert headers["Authorization"] == f"Bearer {ODY_TOKEN}" and ODY_TOKEN not in path


def test_create_and_delete(ody) -> None:
    c = cal(ody)
    uid = c.create(a.CalAdd("Call Tom", "2026-10-21T10:00:00+02:00",
                            "2026-10-21T10:30:00+02:00", False))
    assert ody.bodies()[-1] == {"summary": "Call Tom", "dtstart": "2026-10-21T10:00:00+02:00",
                                "dtend": "2026-10-21T10:30:00+02:00", "all_day": False,
                                "calendar_href": "cal-caldav"}
    cal(ody, href=None).create(a.CalAdd("Rent", "2026-10-31", None, True))
    assert ody.bodies()[-1] == {"summary": "Rent", "dtstart": "2026-10-31", "all_day": True}
    c.delete(uid)
    assert uid not in ody.events and ody.requests[-1][0] == "DELETE"


def test_runner_results_with_odysseus(api_server, ody) -> None:
    r = a.ActionRunner(server_api(api_server), cal(ody))
    assert r.run(1, b'{"t":"cal_delete","id":"u1"}').result == 0
    assert r.run(1, b'{"t":"cal_delete","id":"u1"}').result == 0  # gone (404): done
    out = r.run(2, b'{"t":"cal_delete","id":"u2::20261022"}')  # would delete the series
    assert out.result == 1 and "u2::20261022" in ody.events
    ody.fail.append(503)
    out = r.run(3, b'{"t":"cal_add","title":"x","start":"2026-10-21","all_day":true}')
    assert out.result is None and out.stop == a.STOP_RETRY
    bad = a.ActionRunner(server_api(api_server), cal(ody, token="ody_wrong"))
    out = bad.run(4, b'{"t":"cal_add","title":"x","start":"2026-10-21","all_day":true}')
    assert out.result is None and out.stop == a.STOP_AUTH


def test_sync_with_odysseus_and_templates(api_server, ody, tmp_path) -> None:
    t5 = FakeT5()
    aid = t5.queue_action({"t": "cal_add", "title": "Call Tom", "start": "2026-10-21",
                           "all_day": True, "from_item": 1})
    t5.drop_on_ack.add(aid)  # the ack is lost: the action comes again

    async def go() -> s.Report:
        t5.reconnect()
        sync = s.PdaSync(server_api(api_server), tz_name="UTC", calendar=cal(ody),
                         templates=["Call", " ", "Meeting"],
                         state=s.PdaState(tmp_path / "pda.json"), clock=lambda: NOW,
                         timeout=2.0)
        return await sync.run(t5)

    assert asyncio.run(go()).outcome == s.LINK_LOST
    rep = asyncio.run(go())
    assert rep.outcome == s.DONE, rep.message
    assert t5.acks == [(aid, 0)]
    assert [b["summary"] for b in ody.bodies()] == ["Call Tom"]  # one event, not two
    feed = t5.feed_json()
    # sorted by start: the all-day "2026-10-21" comes before 09:00 that day
    assert [e["title"] for e in feed["calendar"]] == ["Call Tom", "Dentist follow-up", "Run"]
    assert feed["templates"] == ["Call", "Meeting"]


def test_templates_are_optional(api_server) -> None:
    api = server_api(api_server)
    assert "templates" not in f.build_feed(api, now=NOW, tz_name="UTC")
    feed = f.build_feed(api, now=NOW, tz_name="UTC", templates=["x" * 60] * 20)
    assert len(feed["templates"]) == f.TEMPLATES_MAX
    assert all(len(t) <= f.TEMPLATE_MAX for t in feed["templates"])


def test_cli_calendar_choice(monkeypatch, tmp_path) -> None:
    import pda_relay

    def args(**kw) -> argparse.Namespace:
        base = {"odysseus_url": None, "odysseus_token_file": None, "calendar_href": None,
                "http_timeout": 3.0}
        return argparse.Namespace(**{**base, **kw})

    monkeypatch.delenv("ODYSSEUS_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_TOKEN", raising=False)
    assert isinstance(pda_relay.make_odysseus(args())[0], a.NoCalendar)
    assert pda_relay.make_odysseus(args())[1:] == (None, None)
    assert pda_relay.make_odysseus(args(odysseus_url="http://127.0.0.1:7010")) is None
    token = tmp_path / "ody"
    token.write_text(ODY_TOKEN + "\n")
    c, todos, insight = pda_relay.make_odysseus(args(odysseus_url="http://127.0.0.1:7010",
                                                     odysseus_token_file=token,
                                                     calendar_href="cal-caldav"))
    assert isinstance(c, OdysseusCalendar) and c.calendar_href == "cal-caldav"
    assert isinstance(todos, OdysseusTodos) and isinstance(insight, OdysseusInsight)
    assert all(ODY_TOKEN not in repr(x) for x in (c, todos, insight))
    monkeypatch.setenv("ODYSSEUS_TOKEN", ODY_TOKEN)
    assert isinstance(pda_relay.make_odysseus(args(odysseus_url="http://127.0.0.1:7010"))[0],
                      OdysseusCalendar)
    assert pda_relay.make_odysseus(args(odysseus_url="ftp://x")) is None
