"""The owner's Odysseus checklists (docs/t5-pda-plan.md §4.4): the todos client, the feed's
`todos`, the `todo_toggle` action and the trimming order, against a fake Odysseus."""

from __future__ import annotations

import asyncio
import json

import pytest
from athena_relay import pda_actions as a
from athena_relay import pda_feed as f
from athena_relay import pda_sync as s
from athena_relay.pda_todos import OdysseusTodos
from ody_fake import ODY_TOKEN, TODOS, FakeOdysseus
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


def todos(o: FakeOdysseus, token: str = ODY_TOKEN) -> OdysseusTodos:
    return OdysseusTodos(o.url, token, timeout=3.0)


def runner(srv: FakeApiServer, o: FakeOdysseus) -> a.ActionRunner:
    return a.ActionRunner(f.HttpApi(srv.url, TOKEN, timeout=3.0), todos=todos(o))


def toggle(note: str, i: int, text: str, done: bool) -> bytes:
    return json.dumps({"t": "todo_toggle", "note": note, "i": i, "text": text,
                       "done": done}).encode()


def toggles(o: FakeOdysseus) -> list[dict]:
    return o.bodies(TODOS)


# ----------------------------------------------------------------- reading and the feed


def test_feed_todos(api_server, ody) -> None:
    feed = f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                        tz_name="UTC", todos=todos(ody))
    assert feed["todos"] == [
        {"id": "n-groceries", "title": "Groceries", "pinned": True, "due": "2026-10-21",
         "items": [{"i": 0, "text": "Milk", "done": False},
                   {"i": 1, "text": "Bread", "done": True},
                   {"i": 2, "text": "Eggs (free range)", "done": False}]},
        {"id": "n-trip", "title": None, "pinned": False, "due": None,
         "items": [{"i": 0, "text": "Passport", "done": False},
                   {"i": 1, "text": "Charger", "done": False}]},
    ]  # plain notes and empty checklists left out, the hub's order kept
    get = next(p for m, p, _, _ in ody.requests if m == "GET")
    assert get == "/api/codex/todos?format=json"


def test_hub_reports_todo_and_goal_notes_as_checklists(ody) -> None:
    # The hub's JSON route reports notes of type checklist, todo and goal all as
    # "checklist" (plan §4.4), so the relay's type filter keeps them whatever their kind.
    ody.notes = [{"id": f"n-{kind}", "title": kind, "type": "checklist", "label": kind,
                  "pinned": False, "due_date": None,
                  "items": [{"text": "Water the plants", "done": False}], "content": None}
                 for kind in ("checklist", "todo", "goal")]
    assert [n["id"] for n in f._todos(todos(ody))] == ["n-checklist", "n-todo", "n-goal"]


def test_feed_todo_caps(ody) -> None:
    ody.notes = [{"id": f"n{k}", "title": "t" * 200, "type": "checklist", "pinned": False,
                  "due_date": "soon", "items": [{"text": f"item {j} " + "x" * 200, "done": False}
                                                for j in range(50)]} for k in range(25)]
    out = f._todos(todos(ody))
    assert len(out) == f.TODO_NOTES_MAX
    assert len(out[0]["items"]) == f.TODO_ITEMS_MAX and out[0]["items"][-1]["i"] == 39
    assert len(out[0]["title"]) <= f.TITLE_MAX and out[0]["due"] is None
    assert all(len(it["text"]) <= f.TODO_TEXT_MAX for it in out[0]["items"])


@pytest.mark.parametrize("mode", ["text", "forbidden"])
def test_no_todos_is_not_an_error(api_server, ody, mode, caplog) -> None:
    ody.todos_mode = mode
    feed = f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                        tz_name="UTC", todos=todos(ody))
    assert "todos" not in feed
    assert ("todos:read" if mode == "forbidden" else "text") in caplog.text
    out = runner(api_server, ody).run(1, toggle("n-groceries", 0, "Milk", True))
    assert out.result is None and out.stop is None  # waits, like an unconfigured calendar


def test_other_todos_failures_fail_the_feed(api_server, ody) -> None:
    ody.fail.append(500)
    with pytest.raises(f.UploadError):
        f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                     tz_name="UTC", todos=todos(ody))


def test_no_odysseus_leaves_todos_out(api_server) -> None:
    feed = f.build_feed(f.HttpApi(api_server.url, TOKEN, timeout=3.0), now=NOW,
                        tz_name="UTC")
    assert "todos" not in feed
    out = a.ActionRunner(f.HttpApi(api_server.url, TOKEN, timeout=3.0)).run(
        1, toggle("n-groceries", 0, "Milk", True))
    assert out.result is None and "not configured" in out.message


# ----------------------------------------------------------------- todo_toggle


@pytest.mark.parametrize("raw", [
    {"t": "todo_toggle", "note": "n1", "i": 0, "text": "Milk", "done": True},
    {"t": "todo_toggle", "note": "n" * 64, "i": 39, "text": "x" * 80, "done": False},
])
def test_valid_toggles(raw) -> None:
    assert isinstance(a.parse_action(json.dumps(raw).encode()), a.TodoToggle)


@pytest.mark.parametrize("change", [
    {"note": ""}, {"note": "n" * 65}, {"note": 5}, {"i": -1}, {"i": "0"}, {"i": True},
    {"i": 1.0}, {"text": ""}, {"text": "x" * 81}, {"done": "yes"}, {"done": 1},
    {"extra": 1},
])
def test_invalid_toggles(change) -> None:
    raw = {"t": "todo_toggle", "note": "n1", "i": 0, "text": "Milk", "done": True, **change}
    with pytest.raises(a.InvalidAction):
        a.parse_action(json.dumps(raw).encode())
    del raw["t"], raw["note"]
    with pytest.raises(a.InvalidAction):  # a missing field
        a.parse_action(json.dumps({"t": "todo_toggle", **raw}).encode())


def test_toggle_sends_the_hubs_body(api_server, ody) -> None:
    out = runner(api_server, ody).run(1, toggle("n-groceries", 0, "Milk", True))
    assert out.result == 0
    assert toggles(ody) == [{"action": "toggle_item", "note_id": "n-groceries", "index": 0,
                             "done": True}]
    assert ody.notes[0]["items"][0]["done"] is True
    hdrs = next(h for m, p, h, _ in ody.requests if m == "POST")
    assert hdrs["Authorization"] == f"Bearer {ODY_TOKEN}"


def test_index_shift_is_matched_by_text(api_server, ody) -> None:
    # On the website an item was inserted at the top after the feed was built.
    ody.notes[0]["items"].insert(0, {"text": "Coffee", "done": False})
    out = runner(api_server, ody).run(1, toggle("n-groceries", 2, "  Eggs (free", True))
    assert out.result == 0 and toggles(ody)[-1]["index"] == 3  # "Eggs  (free range)"


def test_unmatched_or_ambiguous_is_rejected(api_server, ody) -> None:
    r = runner(api_server, ody)
    assert r.run(1, toggle("n-groceries", 0, "Butter", True)).result == 1
    ody.notes[2]["items"].append({"text": "Passport copy", "done": False})
    r.begin_sync()
    assert r.run(2, toggle("n-trip", 5, "Pass", True)).result == 1  # two items match
    assert r.run(3, toggle("n-gone", 0, "Milk", True)).result == 1
    assert toggles(ody) == []


def test_already_in_state_needs_no_call(api_server, ody) -> None:
    r = runner(api_server, ody)
    assert r.run(1, toggle("n-groceries", 1, "Bread", True)).result == 0  # already done
    assert r.run(2, toggle("n-groceries", 0, "Milk", False)).result == 0  # already open
    assert toggles(ody) == []


def test_exit_code_1_is_rejected(api_server, ody) -> None:
    ody.toggle_exit_code = 1
    assert runner(api_server, ody).run(1, toggle("n-groceries", 0, "Milk", True)).result == 1


def test_temporary_failures_wait(api_server, ody) -> None:
    r = runner(api_server, ody)
    ody.fail.append(503)  # the read fails
    out = r.run(1, toggle("n-groceries", 0, "Milk", True))
    assert out.result is None and out.stop == a.STOP_RETRY
    r.run(2, b'{"t":"item_status","id":1,"status":"done"}')  # (read happens on the next try)
    out = r.run(3, toggle("n-groceries", 0, "Milk", True))
    assert out.result == 0


def test_notes_read_once_per_sync(api_server, ody) -> None:
    t5 = FakeT5()
    first = t5.queue_action({"t": "todo_toggle", "note": "n-groceries", "i": 0,
                             "text": "Milk", "done": True})
    second = t5.queue_action({"t": "todo_toggle", "note": "n-groceries", "i": 0,
                              "text": "Milk", "done": False})  # tapped again: the reverse
    sync = s.PdaSync(f.HttpApi(api_server.url, TOKEN, timeout=3.0), tz_name="UTC",
                     todos=todos(ody), clock=lambda: NOW, timeout=2.0)

    async def go() -> s.Report:
        t5.reconnect()
        return await sync.run(t5)

    rep = asyncio.run(go())
    assert rep.outcome == s.DONE, rep.message
    assert t5.acks == [(first, 0), (second, 0)]
    assert [b["done"] for b in toggles(ody)] == [True, False]
    reads = [p for m, p, _, _ in ody.requests if m == "GET" and p.startswith(TODOS)]
    assert len(reads) == 2  # once for the actions, once for the feed
    assert t5.feed_json()["todos"][0]["items"][0]["done"] is False


# ----------------------------------------------------------------- trimming (§4.1)


def _note(nid: str, pinned: bool, done: list[bool]) -> dict:
    return {"id": nid, "title": nid, "pinned": pinned, "due": None,
            "items": [{"i": k, "text": "x" * 100, "done": d} for k, d in enumerate(done)]}


def test_trimming_order() -> None:
    def feed() -> dict:
        return {"v": 1, "version": 1, "conversations": [],
                "actions": [{"id": k, "text": "a" * 100} for k in range(3)],
                "calendar": [{"id": "e", "title": "c" * 100}],
                "todos": [_note("pinned", True, [False, True]),
                          _note("loose", False, [True, False])]}

    full = len(f.encode_feed(feed()))
    item = len(json.dumps({"i": 0, "text": "x" * 100, "done": True}, separators=(",", ":"))) + 1
    # 1) done items go first, the last checklist's first
    got = json.loads(f.encode_feed(fd := feed(), limit=full - item))
    assert [it["done"] for it in got["todos"][1]["items"]] == [False]
    assert [it["done"] for it in got["todos"][0]["items"]] == [False, True]
    assert fd["actions"] and len(got["actions"]) == 3
    # 2) then the last action items, 3) then unpinned checklists, 4) then the calendar
    got = json.loads(f.encode_feed(feed(), limit=full - 2 * item - 100))
    assert all(not it["done"] for n in got["todos"] for it in n["items"])
    assert len(got["actions"]) == 2 and len(got["todos"]) == 2
    got = json.loads(f.encode_feed(feed(), limit=full - 2 * item - 400))
    assert got["actions"] == [] and [n["id"] for n in got["todos"]] == ["pinned"]
    assert got["calendar"]
    got = json.loads(f.encode_feed(feed(), limit=full - 2 * item - 600))
    assert got["calendar"] == [] and [n["id"] for n in got["todos"]] == ["pinned"]
