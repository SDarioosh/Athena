"""The hub's rule weights in the T5 feed (docs/t5-pda-plan.md §4.1 `weights`, §5): the
client, the feed's `weights` (only `list:` keys with integers, clamped, at most 32), the last
good table kept across failures and restarts, and the case that leaves it out. Fictional
data only."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from athena_relay import pda_feed as f
from athena_relay import pda_sync as s
from athena_relay.pda_calendar import OdysseusCalendar
from athena_relay.pda_insight import OdysseusInsight
from athena_relay.pda_todos import OdysseusTodos
from ody_fake import ODY_TOKEN, WEIGHTS, FakeOdysseus
from pda_fakes import NOW, TOKEN, FakeApiServer, FakeT5

TABLE = {"version": 1, "date": "2026-10-20",
         "weights": {"list:n-trip": 2, "list:n-groceries": -1}}


@pytest.fixture
def ody():
    o = FakeOdysseus()
    o.weights_mode = "json"
    yield o
    o.close()


@pytest.fixture
def api_server():
    srv = FakeApiServer()
    yield srv
    srv.close()


def build(srv: FakeApiServer, o: FakeOdysseus, **kw: Any) -> dict[str, Any]:
    sources = {"calendar": OdysseusCalendar(o.url, ODY_TOKEN, timeout=3.0),
               "todos": OdysseusTodos(o.url, ODY_TOKEN, timeout=3.0),
               "insight": OdysseusInsight(o.url, ODY_TOKEN, timeout=3.0)}
    return f.build_feed(f.HttpApi(srv.url, TOKEN, timeout=3.0), now=NOW, tz_name="UTC",
                        **{**sources, **kw})


def test_the_feed_carries_the_hubs_table(api_server, ody) -> None:
    feed = build(api_server, ody)
    assert feed["weights"] == TABLE
    assert list(feed)[-2:] == ["insight", "weights"]
    get = next((p, h) for m, p, h, _ in ody.requests if p == WEIGHTS)
    assert get[1]["Authorization"] == f"Bearer {ODY_TOKEN}" and ODY_TOKEN not in get[0]
    changed = json.loads(json.dumps(feed))
    changed["weights"]["weights"]["list:n-trip"] = 3
    assert f.content_hash(changed) != f.content_hash(feed)  # a new table is pushed


def test_no_weights_yet_is_an_empty_table(api_server, ody) -> None:
    ody.weights = {"version": 1, "date": "2026-10-20", "weights": {}}
    assert build(api_server, ody, last_weights=TABLE)["weights"] == {
        "version": 1, "date": "2026-10-20", "weights": {}}  # replaces the last one


def test_only_list_keys_with_integers_clamped_and_capped() -> None:
    raw = {"version": 1, "date": "2026-10-20", "extra": True, "weights": {
        "list:a": 9, "list:b": -12, "list:c": 0, "list:d": True, "list:e": 1.0,
        "list:f": "2", "list:g": None, "note_type:checklist": 2, "event_type:work": 1,
        "list:": 3, "LIST:h": 1, "list:" + "x" * 151: 1, "list:" + "y" * 150: 1,
        "list:ü": -6}}
    got = f.weights_section(raw)
    assert got == {"version": 1, "date": "2026-10-20", "weights": {
        "list:a": 6, "list:b": -6, "list:c": 0, "list:" + "y" * 150: 1, "list:ü": -6}}
    many = {"weights": {f"list:n{k}": (k % 13) - 6 for k in range(40)}}
    got = f.weights_section(many)
    assert list(got["weights"]) == [f"list:n{k}" for k in range(32)]  # the hub's order
    assert "date" not in got
    assert f.weights_section({"weights": {}, "date": "20.10.2026"}) == {"version": 1,
                                                                          "weights": {}}


@pytest.mark.parametrize("answer", [
    {"version": 2, "weights": {"list:a": 1}}, {"weights": ["list:a"]}, {"date": "2026-10-20"},
    ["not", "an", "object"], None,
])
def test_malformed_answers(answer) -> None:
    assert f.weights_section(answer) is None


@pytest.mark.parametrize("mode", ["missing", "forbidden", "error", "text"])
def test_a_failure_keeps_the_last_good_table(api_server, ody, caplog, mode) -> None:
    caplog.set_level(logging.INFO, "athena_relay.pda")
    ody.weights_mode = mode
    feed = build(api_server, ody, last_weights=TABLE)
    assert feed["weights"] == TABLE and "insight" in feed  # never fails the feed
    assert "Odysseus weights" in caplog.text
    # never fetched: the key is left out
    assert "weights" not in build(api_server, ody)


def test_a_malformed_answer_keeps_the_last_good_table(api_server, ody, caplog) -> None:
    ody.weights = {"version": 1, "weights": "list:n-trip=2"}
    assert build(api_server, ody, last_weights=TABLE)["weights"] == TABLE
    assert "malformed" in caplog.text


def test_network_errors_keep_the_last_good_table(api_server) -> None:
    gone = OdysseusInsight("http://127.0.0.1:9", ODY_TOKEN, timeout=0.5)
    assert gone.weights() is None


def test_no_odysseus_leaves_it_out(api_server) -> None:
    feed = f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                        tz_name="UTC", last_weights=TABLE)
    assert "weights" not in feed


def test_trimming_drops_weights_after_the_insight() -> None:
    def feed() -> dict:
        return {"v": 1, "version": 1, "conversations": [],
                "actions": [{"id": k, "text": "a" * 100} for k in range(3)],
                "insight": {"date": "2026-10-20", "headline": "h" * 150, "focus": [],
                            "coming_up": [], "also": []},
                "weights": {"version": 1, "weights": {f"list:n{k}": 1 for k in range(10)}}}

    full = len(f.encode_feed(feed()))
    got = json.loads(f.encode_feed(feed(), limit=full - 1))
    assert "insight" not in got and "weights" in got and len(got["actions"]) == 3
    ins = len(json.dumps(feed()["insight"], separators=(",", ":"))) + len(',"insight":')
    got = json.loads(f.encode_feed(feed(), limit=full - ins - 20))
    assert "insight" not in got and "weights" not in got and len(got["actions"]) == 3


def _sync(api_server, ody, state: s.PdaState) -> s.PdaSync:
    return s.PdaSync(f.HttpApi(api_server.url, TOKEN, timeout=3.0), tz_name="UTC",
                     calendar=OdysseusCalendar(ody.url, ODY_TOKEN, timeout=3.0),
                     todos=OdysseusTodos(ody.url, ODY_TOKEN, timeout=3.0),
                     insight=OdysseusInsight(ody.url, ODY_TOKEN, timeout=3.0),
                     state=state, clock=lambda: NOW, timeout=2.0)


def _run(sync: s.PdaSync, t5: FakeT5) -> s.Report:
    async def go() -> s.Report:
        t5.reconnect()
        return await sync.run(t5)

    return asyncio.run(go())


def test_the_last_good_table_survives_a_restart(api_server, ody, tmp_path: Path) -> None:
    path = tmp_path / "pda.json"
    t5 = FakeT5()
    rep = _run(_sync(api_server, ody, s.PdaState(path)), t5)
    assert rep.outcome == s.DONE, rep.message
    assert t5.feed_json()["weights"] == TABLE
    assert json.loads(path.read_text())["weights"] == TABLE
    # restarted, and the hub's route now fails: the T5 still gets the last good table
    ody.weights_mode = "error"
    ody.insight["headline"] = "Another day"  # so a new feed is pushed
    rep = _run(_sync(api_server, ody, s.PdaState(path)), t5)
    assert rep.outcome == s.DONE, rep.message
    assert rep.feed_pushed
    assert t5.feed_json()["weights"] == TABLE
    assert t5.feed_json()["insight"]["headline"] == "Another day"
    # a broken state file is checked again: a bad table is not sent
    path.write_text(json.dumps({"weights": {"weights": "bad"}}))
    assert s.PdaState(path).weights is None
