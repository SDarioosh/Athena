"""The hub's Today insight in the T5 feed (docs/t5-pda-plan.md §4.1): the client, the
feed's `insight` (copied fields, caps, refs that must resolve), the cases that leave it
out, and its place in the trimming order. Fictional data only."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from athena_relay import pda_feed as f
from athena_relay import pda_sync as s
from athena_relay.pda_calendar import OdysseusCalendar
from athena_relay.pda_insight import (
    MALFORMED,
    NO_SCOPE,
    NOT_YET,
    OLD_HUB,
    OdysseusInsight,
)
from athena_relay.pda_todos import OdysseusTodos
from athena_relay.uploader import AUTH, RETRY
from ody_fake import INSIGHT, ODY_TOKEN, FakeOdysseus
from pda_fakes import NOW, TOKEN, FakeApiServer, FakeT5


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


def insight(o: FakeOdysseus, token: str = ODY_TOKEN) -> OdysseusInsight:
    return OdysseusInsight(o.url, token, timeout=3.0)


def build(srv: FakeApiServer, o: FakeOdysseus, **kw: Any) -> dict[str, Any]:
    sources = {"calendar": OdysseusCalendar(o.url, ODY_TOKEN, timeout=3.0),
               "todos": OdysseusTodos(o.url, ODY_TOKEN, timeout=3.0), "insight": insight(o)}
    return f.build_feed(f.HttpApi(srv.url, TOKEN, timeout=3.0), now=NOW, tz_name="UTC",
                        **{**sources, **kw})


EXPECTED = {
    "date": "2026-10-20",
    "headline": "A light day: the dentist in the morning, then the groceries.",
    "focus": [{"ref": "todo:n-groceries:0", "why": "Needed for breakfast tomorrow"},
              {"ref": "event:u1", "why": "Bring the referral letter"}],
    "coming_up": [{"ref": "event:u2::20261022", "why": "Pack the running shoes"}],
    "also": [{"ref": "todo:n-trip:1", "why": "Before the trip"}],
}


# ----------------------------------------------------------------- the feed's insight


def test_sync_puts_the_insight_in_the_feed(api_server, ody) -> None:
    t5 = FakeT5()
    sync = s.PdaSync(f.HttpApi(api_server.url, TOKEN, timeout=3.0), tz_name="UTC",
                     calendar=OdysseusCalendar(ody.url, ODY_TOKEN, timeout=3.0),
                     todos=OdysseusTodos(ody.url, ODY_TOKEN, timeout=3.0),
                     insight=insight(ody), clock=lambda: NOW, timeout=2.0)

    async def go() -> s.Report:
        t5.reconnect()
        return await sync.run(t5)

    rep = asyncio.run(go())
    assert rep.outcome == s.DONE, rep.message
    feed = t5.feed_json()
    assert feed["insight"] == EXPECTED  # no `title`, no `generated`
    assert list(feed)[-3:] == ["calendar", "todos", "insight"]
    get = next((p, h) for m, p, h, _ in ody.requests if p == INSIGHT)
    assert get[1]["Authorization"] == f"Bearer {ODY_TOKEN}" and ODY_TOKEN not in get[0]
    # The push hash covers the insight like every other key.
    changed = json.loads(json.dumps(feed))
    changed["insight"]["headline"] = "Another day"
    assert f.content_hash(changed) != f.content_hash(feed)


def test_refs_must_resolve_in_the_feed(api_server, ody) -> None:
    good = [{"ref": "todo:n-groceries:2", "why": "ok"}, {"ref": "event:u1", "why": "ok"}]
    ody.insight["focus"] = [
        good[0],
        {"ref": "todo:n-groceries:3", "why": "no item 3"},
        {"ref": "todo:n-groceries:x", "why": "not an integer"},
        {"ref": "todo:n-groceries:01", "why": "not the feed's index"},
        {"ref": "todo:n-groceries:-1", "why": "negative"},
        {"ref": "todo:n-unknown:0", "why": "no such note"},
        {"ref": "todo:n-idea:0", "why": "a plain note"},
        {"ref": "todo:n-empty:0", "why": "an empty checklist (not in the feed)"},
        {"ref": "todo:n-groceries", "why": "no index"},
        {"ref": "event:u9", "why": "no such event"},
        {"ref": "event:", "why": "no id"},
        {"ref": "conv:c1", "why": "not a todo or event"},
        {"ref": 5, "why": "not a string"},
        {"why": "no ref"},
        "event:u1",
        good[1],
    ]
    ody.insight["coming_up"] = [{"ref": "event:u2", "why": "the series, not the occurrence"}]
    out = build(api_server, ody)["insight"]
    assert out["focus"] == good
    assert out["coming_up"] == []


def test_event_refs_need_the_calendar_and_todo_refs_the_todos(api_server, ody) -> None:
    feed = build(api_server, ody, calendar=None, todos=None)
    assert "calendar" in feed and feed["calendar"] == [] and "todos" not in feed
    assert feed["insight"] == {**EXPECTED, "focus": [], "coming_up": [], "also": []}


def test_caps(api_server, ody) -> None:
    ody.insight = {
        "date": "2026-10-20",
        "headline": "word " * 80,
        "focus": [{"ref": "event:u1", "why": "why " * 40}] * 3
        + [{"ref": "todo:n-groceries:0"}, {"ref": "todo:n-groceries:1", "why": 7},
           {"ref": "todo:n-trip:0", "why": "  two   spaces "}, {"ref": "event:u1", "why": "6th"}],
        "coming_up": "not a list",
        # "also" is missing
        "extra": {"ignored": True},
    }
    out = build(api_server, ody)["insight"]
    assert len(out["headline"]) <= f.INSIGHT_HEADLINE_MAX and out["headline"].endswith("…")
    assert len(out["focus"]) == f.INSIGHT_ITEMS_MAX  # the first five kept
    assert all(len(it["why"]) <= f.INSIGHT_WHY_MAX for it in out["focus"])
    assert out["focus"][0]["why"].endswith("…")
    assert [it["why"] for it in out["focus"][3:]] == ["", ""]  # missing, not a string
    assert out["coming_up"] == [] and out["also"] == []
    assert list(out) == ["date", "headline", "focus", "coming_up", "also"]


# ----------------------------------------------------------------- left out, not a failure


@pytest.mark.parametrize(("mode", "why"), [
    ("none", NOT_YET), ("forbidden", NO_SCOPE), ("missing", OLD_HUB), ("text", MALFORMED),
])
def test_no_insight_leaves_it_out(api_server, ody, caplog, mode, why) -> None:
    caplog.set_level(logging.INFO, "athena_relay.pda")
    ody.insight_mode = mode
    feed = build(api_server, ody)
    assert "insight" not in feed and feed["todos"] and feed["calendar"]
    assert why in caplog.text


@pytest.mark.parametrize("answer", [
    {"headline": "no date"},
    {"date": "20.10.2026", "headline": "bad date"},
    {"date": "2026-10-20T05:00:00Z", "headline": "a time, not a date"},
    {"date": "２０２６-10-20", "headline": "not ASCII digits"},
    {"date": 20261020, "headline": "a number"},
    {"date": "2026-10-20"},
    {"date": "2026-10-20", "headline": None},
    {"date": "2026-10-20", "headline": ["a list"]},
    ["not", "an", "object"],
])
def test_malformed_answer_leaves_it_out(api_server, ody, caplog, answer) -> None:
    ody.insight = answer
    assert "insight" not in build(api_server, ody)
    assert MALFORMED in caplog.text


@pytest.mark.parametrize(("status", "why"), [
    (405, OLD_HUB), (400, "Odysseus insight: HTTP 400"), (410, "Odysseus insight: HTTP 410"),
])
def test_client_other_final_answers_are_no_insight(ody, caplog, status, why) -> None:
    ody.fail.append(status)
    assert insight(ody).today() is None
    assert why in caplog.text


def test_no_odysseus_leaves_it_out(api_server) -> None:
    feed = f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                        tz_name="UTC")
    assert "insight" not in feed


def test_temporary_failures_fail_the_feed_like_the_calendar(api_server, ody) -> None:
    ody.insight_mode = "error"  # 503
    with pytest.raises(f.UploadError) as e:
        build(api_server, ody)
    assert e.value.kind == RETRY
    with pytest.raises(f.UploadError) as e:  # a rejected token (401) too
        insight(ody, token="ody_wrong").today()
    assert e.value.kind == AUTH and e.value.status == 401


# ----------------------------------------------------------------- trimming (§4.1)


def _note(nid: str, done: list[bool]) -> dict:
    return {"id": nid, "title": nid, "pinned": True, "due": None,
            "items": [{"i": k, "text": "x" * 100, "done": d} for k, d in enumerate(done)]}


def _dumped(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_trimming_drops_insight_after_done_items_before_actions() -> None:
    def feed() -> dict:
        return {"v": 1, "version": 1, "conversations": [],
                "actions": [{"id": k, "text": "a" * 100} for k in range(3)],
                "calendar": [{"id": "e", "title": "c" * 100}],
                "todos": [_note("n", [False, True])],
                "insight": {"date": "2026-10-20", "headline": "h" * 150,
                            "focus": [{"ref": "todo:n:1", "why": "done one"},
                                      {"ref": "todo:n:0", "why": "open one"}],
                            "coming_up": [{"ref": "event:e", "why": "soon"}], "also": []}}

    full = len(f.encode_feed(feed()))
    item = _dumped({"i": 1, "text": "x" * 100, "done": True}) + 1
    ins = _dumped(feed()["insight"]) + len(',"insight":')
    # 1) the done item goes first; the insight stays but loses the ref to it
    got = json.loads(f.encode_feed(feed(), limit=full - 1))
    assert [it["i"] for it in got["todos"][0]["items"]] == [0]
    assert got["insight"]["focus"] == [{"ref": "todo:n:0", "why": "open one"}]
    assert got["insight"]["coming_up"] == [{"ref": "event:e", "why": "soon"}]
    assert len(got["actions"]) == 3
    # 2) then the whole insight, before any action item
    got = json.loads(f.encode_feed(feed(), limit=full - item - 10))
    assert "insight" not in got and len(got["actions"]) == 3
    # 3) then the action items
    got = json.loads(f.encode_feed(feed(), limit=full - item - ins - 10))
    assert "insight" not in got and len(got["actions"]) == 2
