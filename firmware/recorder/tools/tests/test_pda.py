"""The T5 PDA glance protocol (docs/t5-pda-plan.md §3-§4): codec, feed, actions and the
relay algorithm against a fake T5 and a fake Athena server."""

from __future__ import annotations

import asyncio
import json
import struct
import zlib

import pytest
from barvis_relay import pda_actions as a
from barvis_relay import pda_feed as f
from barvis_relay import pda_protocol as q
from barvis_relay import pda_sync as s
from barvis_relay.uploader import RETRY, UploadError
from pda_fakes import NOW, TOKEN, FakeApiServer, FakeCalendar, FakeT5


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def api_server():
    srv = FakeApiServer()
    yield srv
    srv.close()


def api(srv: FakeApiServer, token: str = TOKEN) -> f.HttpApi:
    return f.HttpApi(srv.url, token, timeout=3.0)


def sync_for(srv: FakeApiServer, *, calendar=None, state=None, clock=lambda: NOW,
             **kw) -> s.PdaSync:
    return s.PdaSync(api(srv), tz_name="UTC", calendar=calendar, state=state, clock=clock,
                     timeout=2.0, sleep=_no_sleep, **kw)


def run(sync: s.PdaSync, t5: FakeT5) -> s.Report:
    async def go() -> s.Report:
        t5.reconnect()
        return await sync.run(t5)

    return asyncio.run(go())


# ----------------------------------------------------------------- codec (§3.2-§3.3)


def test_command_bytes() -> None:
    assert q.encode_time(0x0102030405060708, -60) == (
        b"\x04\x00\x00\x00" + struct.pack("<Q", 0x0102030405060708) + struct.pack("<h", -60))
    assert q.encode_put_begin(1, 7, 300, 0xDEADBEEF) == (
        b"\x10\x01\x00\x00" + struct.pack("<III", 7, 300, 0xDEADBEEF))
    assert q.encode_put_data(512, b"ab") == b"\x11\x00\x02\x00\x00ab"
    assert q.encode_put_end() == b"\x12"
    assert q.encode_ob_list(5, 16) == b"\x20\x05\x00\x00\x00\x10\x00"
    assert q.encode_ob_ack(9, 1) == b"\x21\x09\x00\x00\x00\x01"
    assert q.encode_bye() == b"\x05"
    assert q.put_payload_size(517) == 507  # 512-byte frames, not MTU - 3 = 514
    assert q.put_payload_size(420) == 412


def test_responses_round_trip_and_bytes() -> None:
    cases = [
        (q.TimeOk(), b"\x86\x00\x00\x00"),
        (q.PutReady(1, 7), b"\x90\x01\x00\x00\x07\x00\x00\x00"),
        (q.PutOk(2, 7), b"\x91\x02\x00\x00\x07\x00\x00\x00"),
        (q.ObEntry(3, 5, b"{}"), b"\xa0\x00\x02\x00\x03\x00\x00\x00" + struct.pack("<Q", 5)
         + b"{}"),
        (q.ObEnd(2, 4), b"\xa1\x00\x02\x00\x04\x00\x00\x00"),
        (q.ObAcked(1, 9), b"\xa2\x01\x00\x00\x09\x00\x00\x00"),
        (q.Error(q.OP_PUT_END, q.ERR_BAD_CRC), b"\xee\x12\x08\x00\x00\x00\x00\x00"),
    ]
    for msg, raw in cases:
        assert msg.encode() == raw
        assert q.decode_response(raw) == msg
    assert str(q.decode_response(cases[-1][1])) == "ERROR bad_crc on PUT_END (arg 0)"
    assert type(q.decode_response(b"\x87")).__name__ == "ByeOk"


def test_bad_responses() -> None:
    for raw in (b"", b"\x99", b"\x90\x01", b"\xa0\x00\x10\x00" + bytes(12) + b"short"):
        with pytest.raises(q.ProtocolError):
            q.decode_response(raw)


def test_status() -> None:
    st = q.Status(1, q.ST_TIME_VALID | q.ST_USER_ACTIVE, -1, 3, 1792486800, 0xABCD)
    raw = st.encode()
    assert len(raw) == 16 and raw[:4] == b"\x01\x11\xff\x00"
    assert raw[4:6] == b"\x03\x00" and raw[8:12] == struct.pack("<I", 1792486800)
    assert q.Status.decode(raw + b"later") == st and st.user_active
    assert "outbox=3" in st.describe() and "?%" in st.describe()
    with pytest.raises(q.ProtocolError):
        q.Status.decode(raw[:15])


def test_outbox_waiting_flag() -> None:
    assert q.outbox_waiting(b"\x01") and not q.outbox_waiting(b"\x00")
    assert not q.outbox_waiting(None) and not q.outbox_waiting(b"")


# ----------------------------------------------------------------- feed (§4.1)


def test_feed_from_api(api_server) -> None:
    feed = f.build_feed(api(api_server), now=NOW, tz_name="UTC", calendar=FakeCalendar())
    assert feed["v"] == 1 and feed["version"] == int(NOW) and feed["tz"] == "UTC"
    assert feed["generated"] == "2026-10-20T09:00:00Z"
    # today's digest is missing (404) -> yesterday's
    assert feed["briefing"]["date"] == "2026-10-19"
    assert feed["briefing"]["text"].startswith("Lease and family dinner\n\n")
    assert [c["id"] for c in feed["conversations"]] == ["c_new", "c_old"]  # no discarded
    assert feed["conversations"][0] == {
        "id": "c_new", "title": "Standup", "at": "2026-10-20T07:30:00Z",
        "people": ["Ben", "Alice"], "summary": "Ben finished the API migration.",
        "points": ["Demo moved"]}
    assert [x["id"] for x in feed["actions"]] == [1, 3, 2]  # due first, undated last
    assert feed["actions"][0] == {"id": 1, "text": "Sign the renewal", "owner": "Me",
                                  "due": "2026-10-31", "conv": "c_old"}
    assert feed["calendar"][0]["id"] == "ev1" and feed["calendar"][0]["where"] == "Dr. Patel"
    gets = api_server.calls("GET")
    assert "/v1/digests/2026-10-20" in gets and "/v1/digests/2026-10-19" in gets
    conv_q = next(p for p in gets if p.startswith("/v1/conversations?"))
    assert "since=2026-10-18T09%3A00%3A00Z" in conv_q and "limit=20" in conv_q
    assert "/v1/action-items?status=open" in gets


def test_feed_without_calendar_or_digest(api_server) -> None:
    api_server.data["digests"].clear()
    feed = f.build_feed(api(api_server), now=NOW, tz_name="UTC", calendar=a.NoCalendar())
    assert "briefing" not in feed and feed["calendar"] == []


def test_feed_source_error_fails_the_build(api_server) -> None:
    api_server.fail["/v1/action-items"] = [503]
    with pytest.raises(UploadError) as e:
        f.build_feed(api(api_server), now=NOW, tz_name="UTC")
    assert e.value.kind == RETRY


def test_feed_size_cap_trims_summaries_then_oldest() -> None:
    convs = [{"id": f"c{i}", "title": "t", "at": f"2026-10-20T{20 - i:02d}:00:00Z",
              "people": [], "summary": "word " * 120, "points": []} for i in range(20)]
    feed = {"v": 1, "version": 1, "conversations": convs,
            "actions": [{"id": 1, "text": "x"}], "calendar": []}
    raw = f.encode_feed(feed, limit=4000)
    assert len(raw) <= 4000
    got = json.loads(raw)
    assert all(len(c["summary"]) <= f.SUMMARY_CUT for c in got["conversations"])
    assert [c["id"] for c in got["conversations"]] == [f"c{i}" for i in range(
        len(got["conversations"]))]  # the oldest (the end of the list) were dropped
    assert got["actions"] == [{"id": 1, "text": "x"}]
    with pytest.raises(f.FeedTooLarge):
        f.encode_feed({"v": 1, "big": "x" * 100}, limit=50)


def test_content_hash_ignores_version() -> None:
    a1 = {"v": 1, "version": 1, "generated": "x", "actions": [1]}
    a2 = {"v": 1, "version": 2, "generated": "y", "actions": [1]}
    assert f.content_hash(a1) == f.content_hash(a2)
    assert f.content_hash(a1) != f.content_hash({**a1, "actions": [2]})


def test_cut() -> None:
    assert f.cut("  a   b ", 10) == "a b"
    out = f.cut("the quick brown fox jumps over", 15)
    assert len(out) <= 15 and out.endswith("…") and out == "the quick…"


# ----------------------------------------------------------------- actions (§4.2)


@pytest.mark.parametrize("raw", [
    {"t": "item_status", "id": 4, "status": "done"},
    {"t": "item_status", "id": 4, "status": "open"},
    {"t": "cal_add", "title": "Call", "start": "2026-10-21T09:00:00+02:00",
     "end": "2026-10-21T09:30:00+02:00"},
    {"t": "cal_add", "title": "Rent", "start": "2026-10-31", "all_day": True, "from_item": 1},
    {"t": "cal_delete", "id": "ev1"},
    {"t": "fetch", "ref": "conv:c_1"},
    {"t": "fetch", "ref": "transcript:c_1"},
    {"t": "fetch", "ref": "transcript:c_1@600-1200"},
    {"t": "digest_refresh", "date": "2026-10-19"},
])
def test_valid_actions(raw) -> None:
    a.parse_action(json.dumps(raw).encode())


@pytest.mark.parametrize("raw", [
    b"not json", b"\xff\xfe", b"[1]", b'{"t": "nope"}', b'{"id": 1}',
    b'{"t": "item_status", "id": 1}',  # missing status
    b'{"t": "item_status", "id": 1, "status": "done", "x": 1}',  # unknown field
    b'{"t": "item_status", "id": "1", "status": "done"}',
    b'{"t": "item_status", "id": true, "status": "done"}',
    b'{"t": "item_status", "id": 0, "status": "done"}',
    b'{"t": "item_status", "id": 1, "status": "deleted"}',
    b'{"t": "cal_add", "title": "x", "start": "2026-10-21T09:00:00+02:00"}',  # no end
    b'{"t": "cal_add", "title": "x", "start": "2026-10-21T09:00:00", '
    b'"end": "2026-10-21T10:00:00"}',  # no offset
    b'{"t": "cal_add", "title": "x", "start": "2026-10-21T10:00:00Z", '
    b'"end": "2026-10-21T09:00:00Z"}',  # end before start
    b'{"t": "cal_add", "title": "x", "start": "2026-10-21", "all_day": true, '
    b'"end": "2026-10-22"}',
    b'{"t": "cal_add", "title": "", "start": "2026-10-21", "all_day": true}',
    b'{"t": "cal_add", "title": "x", "start": "2026-02-30", "all_day": true}',
    b'{"t": "cal_delete", "id": 5}',
    b'{"t": "fetch", "ref": "conv:../../etc"}',
    b'{"t": "fetch", "ref": "conv:c_1@0-600"}',
    b'{"t": "fetch", "ref": "transcript:c_1@600-600"}',
    b'{"t": "digest_refresh", "date": "yesterday"}',
    b'{"t": "item_status", "id": 1, "status": "done", "pad": "' + b"x" * 400 + b'"}',
])
def test_invalid_actions(raw) -> None:
    with pytest.raises(a.InvalidAction):
        a.parse_action(raw)


def test_runner_results(api_server) -> None:
    r = a.ActionRunner(api(api_server), FakeCalendar())
    assert r.run(1, b'{"t":"item_status","id":1,"status":"done"}').result == 0
    assert api_server.data["action_items"][1]["status"] == "done"
    assert r.run(2, b'{"t":"item_status","id":99,"status":"done"}').result == 1  # 404
    assert r.run(3, b"garbage").result == 1
    assert r.run(4, b'{"t":"digest_refresh","date":"2026-10-19"}').result == 0
    assert r.run(5, b'{"t":"digest_refresh","date":"2026-01-01"}').result == 1
    api_server.fail["/v1/action-items"] = [502]
    out = r.run(6, b'{"t":"item_status","id":2,"status":"done"}')
    assert out.result is None and out.stop == a.STOP_RETRY
    bad = a.ActionRunner(api(api_server, token="wrong-token-123"), FakeCalendar())
    out = bad.run(7, b'{"t":"item_status","id":2,"status":"done"}')
    assert out.result is None and out.stop == a.STOP_AUTH


def test_calendar_actions_and_dedupe(api_server) -> None:
    cal = FakeCalendar()
    r = a.ActionRunner(api(api_server), cal)
    add = b'{"t":"cal_add","title":"Call","start":"2026-10-21","all_day":true}'
    assert r.run(10, add).result == 0
    assert r.run(10, add).result == 0  # re-sent after a lost ack: no second event
    assert len(cal.created) == 1
    assert r.run(11, add).result == 0 and len(cal.created) == 2  # a new action id
    assert r.run(12, b'{"t":"cal_delete","id":"ev1"}').result == 0
    assert r.run(12, b'{"t":"cal_delete","id":"ev1"}').result == 0  # already gone: done
    cal.fail.append(UploadError(RETRY, "calendar down", 503))
    out = r.run(13, b'{"t":"cal_delete","id":"ev2"}')
    assert out.result is None and out.stop == a.STOP_RETRY


def test_calendar_not_configured_waits(api_server) -> None:
    r = a.ActionRunner(api(api_server))  # NoCalendar
    out = r.run(1, b'{"t":"cal_delete","id":"ev1"}')
    assert out.result is None and out.stop is None and "not configured" in out.message


def test_fetch_details(api_server) -> None:
    r = a.ActionRunner(api(api_server))
    conv = r.run(1, b'{"t":"fetch","ref":"conv:c_new"}')
    assert conv.result == 0 and conv.detail["ref"] == "conv:c_new"
    assert conv.detail["summary"] == "Ben finished the API migration."
    assert conv.detail["actions"] == [{"id": 2, "text": "Review Ben's PR", "status": "open",
                                       "due": None}]
    page = r.run(2, b'{"t":"fetch","ref":"transcript:c_new"}').detail
    assert page["from"] == 0 and page["to"] == 600
    assert page["segments"][0] == {"o": 0.0, "s": "Me", "me": True, "x": "line at 0 s"}
    assert all(0 <= x["o"] < 600 for x in page["segments"])
    assert page["next"] == "transcript:c_new@600-1200"
    page2 = r.run(3, json.dumps({"t": "fetch", "ref": page["next"]}).encode()).detail
    assert page2["segments"][0]["o"] == 600.0 and page2["next"] == "transcript:c_new@1200-1800"
    last = r.run(4, b'{"t":"fetch","ref":"transcript:c_new@1200-1800"}').detail
    assert last["next"] is None and last["segments"][-1]["o"] == 1296.0
    assert r.run(5, b'{"t":"fetch","ref":"conv:c_missing"}').result == 1


# ----------------------------------------------------------------- relay (§3.4)


def test_full_sync(api_server) -> None:
    t5 = FakeT5()
    done = t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
    bad = t5.queue_action(b'{"t":"rm -rf"}')
    fetch = t5.queue_action({"t": "fetch", "ref": "conv:c_new"})
    rep = run(sync_for(api_server, calendar=FakeCalendar()), t5)

    assert rep.outcome == s.DONE, rep.message
    assert t5.acks == [(done, 0), (bad, 1), (fetch, 0)] and not t5.outbox
    assert api_server.data["action_items"][1]["status"] == "done"
    ops = t5.opcodes()
    assert ops[0] == q.OP_TIME and ops[1] == q.OP_OB_LIST and ops[-1] == q.OP_BYE
    assert t5.clock_ms == int(NOW * 1000) and t5.tz_offset_min == 0
    # outbox first, then the feed, then the detail, then its ack
    assert t5.puts == [(q.KIND_FEED, int(NOW)), (q.KIND_DETAIL, fetch)]
    assert ops.index(q.OP_PUT_BEGIN) > ops.index(q.OP_OB_ACK)
    feed = t5.feed_json()
    assert [x["id"] for x in feed["actions"]] == [3, 2]  # item 1 is done now
    assert t5.feed_version == feed["version"] == int(NOW)
    assert json.loads(t5.details[fetch])["ref"] == "conv:c_new"
    assert rep.feed_pushed == [int(NOW)] and rep.details == ["conv:c_new"]


def test_put_chunks_fit_the_mtu(api_server) -> None:
    t5 = FakeT5(mtu=420)
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE, rep.message
    datas = [c for c in t5.commands if c[0] == q.OP_PUT_DATA]
    assert len(datas) > 1 and all(len(c) <= 417 for c in datas)
    offsets = [int.from_bytes(c[1:5], "little") for c in datas]
    assert offsets == list(range(0, len(t5.feed), 412))
    begin = next(c for c in t5.commands if c[0] == q.OP_PUT_BEGIN)
    assert int.from_bytes(begin[12:16], "little") == zlib.crc32(t5.feed)


def test_put_frames_at_mtu_517_stay_within_512(api_server) -> None:
    t5 = FakeT5(mtu=517)  # what Android negotiates; the fake refuses writes over 512
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE, rep.message
    datas = [c for c in t5.commands if c[0] == q.OP_PUT_DATA]
    assert len(datas) > 1 and max(len(c) for c in datas) == 512
    assert b"".join(c[5:] for c in datas) == t5.feed


def test_unchanged_feed_is_not_pushed_again(api_server, tmp_path) -> None:
    state = s.PdaState(tmp_path / "pda.json")
    t5 = FakeT5()
    assert run(sync_for(api_server, state=state), t5).feed_pushed == [int(NOW)]
    # another run (new process: state from the file), a minute later, nothing changed
    rep = run(sync_for(api_server, state=s.PdaState(tmp_path / "pda.json"),
                       clock=lambda: NOW + 60), t5)
    assert rep.feed_pushed == [] and rep.feed_unchanged == 1
    # the content changes -> pushed, with a newer version
    api_server.data["action_items"][3]["status"] = "done"
    rep = run(sync_for(api_server, state=state, clock=lambda: NOW + 120), t5)
    assert rep.feed_pushed == [int(NOW) + 120]
    # the T5 lost its feed (wiped): pushed again although the content is the same
    t5.feed, t5.feed_version = None, 0
    rep = run(sync_for(api_server, state=state, clock=lambda: NOW + 180), t5)
    assert rep.feed_pushed == [int(NOW) + 180] and t5.feed is not None


def test_feed_version_only_grows(api_server) -> None:
    t5 = FakeT5(feed_version=int(NOW) + 500)  # pushed by a relay whose clock is ahead
    rep = run(sync_for(api_server), t5)
    assert rep.feed_pushed == [int(NOW) + 501]
    assert t5.feed_json()["version"] == int(NOW) + 501


def test_server_down_leaves_everything(api_server) -> None:
    api_server.health_status = 503
    t5 = FakeT5()
    t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.SERVER_RETRY
    assert t5.opcodes() == [q.OP_TIME, q.OP_BYE] and t5.outbox and t5.feed is None


def test_temporary_failure_is_not_acked_and_retries(api_server) -> None:
    t5 = FakeT5()
    first = t5.queue_action({"t": "item_status", "id": 2, "status": "done"})
    second = t5.queue_action({"t": "item_status", "id": 3, "status": "dismissed"})
    api_server.fail["/v1/action-items/2"] = [500]
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.SERVER_RETRY and t5.acks == [] and len(t5.outbox) == 2
    assert t5.feed is None  # stopped: no feed from a failing server
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE and t5.acks == [(first, 0), (second, 0)]


def test_lost_ack_retries_without_duplicates(api_server) -> None:
    """Milestone 3: the server's PATCH happened, OB_ACKED was lost; the action comes again
    and the second PATCH is harmless (same status)."""
    t5 = FakeT5()
    aid = t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
    t5.drop_on_ack.add(aid)
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.LINK_LOST and aid in t5.outbox
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE and t5.acks == [(aid, 0)]
    assert api_server.calls("PATCH") == ["/v1/action-items/1"] * 2
    assert api_server.data["action_items"][1]["status"] == "done"


def test_lost_ack_of_cal_add_creates_one_event(api_server, tmp_path) -> None:
    cal = FakeCalendar()
    t5 = FakeT5()
    aid = t5.queue_action({"t": "cal_add", "title": "Call Tom", "start": "2026-10-21",
                           "all_day": True})
    t5.drop_on_ack.add(aid)
    assert run(sync_for(api_server, calendar=cal, state=s.PdaState(tmp_path / "p.json")),
               t5).outcome == s.LINK_LOST
    rep = run(sync_for(api_server, calendar=cal, state=s.PdaState(tmp_path / "p.json")), t5)
    assert rep.outcome == s.DONE and t5.acks == [(aid, 0)] and len(cal.created) == 1
    assert any(e["title"] == "Call Tom" for e in t5.feed_json()["calendar"])


def test_calendar_action_waits_without_calendar(api_server) -> None:
    t5 = FakeT5()
    cal = t5.queue_action({"t": "cal_delete", "id": "ev1"})
    item = t5.queue_action({"t": "item_status", "id": 2, "status": "done"})
    rep = run(sync_for(api_server, calendar=a.NoCalendar()), t5)
    assert rep.outcome == s.DONE and t5.acks == [(item, 0)] and cal in t5.outbox
    assert cal in rep.waiting and t5.feed_json()["calendar"] == []


def test_outbox_paging(api_server) -> None:
    t5 = FakeT5()
    ids = [t5.queue_action({"t": "item_status", "id": 2, "status": "done"})
           for _ in range(q.OB_LIST_MAX + 5)]
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE and [x for x, _ in t5.acks] == ids
    assert t5.opcodes().count(q.OP_OB_LIST) == 2


def test_bad_crc_is_sent_again(api_server) -> None:
    t5 = FakeT5(bad_crc_next=1)
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE and t5.opcodes().count(q.OP_PUT_BEGIN) == 2 and t5.feed


def test_bad_crc_every_time_gives_up(api_server) -> None:
    t5 = FakeT5(bad_crc_next=s.PUT_ATTEMPTS)
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DEVICE_RETRY and "bad_crc" in rep.message and t5.feed is None


def test_mtu_too_small(api_server) -> None:
    rep = run(sync_for(api_server), FakeT5(mtu=200))
    assert rep.outcome == s.DEVICE_RETRY and "MTU is 200" in rep.message


def test_busy_is_retried(api_server) -> None:
    t5 = FakeT5(busy_next=2)
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.DONE and t5.opcodes()[:3] == [q.OP_TIME] * 3


def test_incompatible_proto(api_server) -> None:
    t5 = FakeT5(info=b"proto=2;device=t5-pda")
    rep = run(sync_for(api_server), t5)
    assert rep.outcome == s.INCOMPATIBLE and t5.opcodes() == [q.OP_BYE]


def test_list_only_changes_nothing(api_server) -> None:
    t5 = FakeT5()
    t5.queue_action({"t": "item_status", "id": 1, "status": "done"})
    rep = run(sync_for(api_server, list_only=True), t5)
    assert rep.outcome == s.DONE and len(rep.listed) == 1
    assert t5.opcodes() == [q.OP_OB_LIST, q.OP_BYE] and t5.outbox and not api_server.requests


def test_stay_connected_while_active(api_server) -> None:
    """§3.4 step 6: an action queued while the user is active is handled at once; when
    the screen goes idle the relay says BYE."""
    t5 = FakeT5(flags=q.ST_TIME_VALID | q.ST_USER_ACTIVE)
    sync = sync_for(api_server, stay=True, status_poll=1.0)
    added: list[int] = []

    async def user() -> None:
        await asyncio.sleep(0.2)
        added.append(t5.queue_action({"t": "item_status", "id": 3, "status": "done"}))
        t5.notify_status()
        await asyncio.sleep(0.5)
        t5.flags &= ~q.ST_USER_ACTIVE
        t5.notify_status()

    async def go() -> s.Report:
        t5.reconnect()
        rep, _ = await asyncio.gather(sync.run(t5), user())
        return rep

    rep = asyncio.run(go())
    assert rep.outcome == s.DONE, rep.message
    assert t5.acks == [(added[0], 0)] and t5.opcodes()[-1] == q.OP_BYE
    assert api_server.data["action_items"][3]["status"] == "done"


def test_stay_connected_refreshes_the_feed(api_server) -> None:
    t5 = FakeT5(flags=q.ST_TIME_VALID | q.ST_USER_ACTIVE)
    clock = [NOW]
    sync = sync_for(api_server, stay=True, clock=lambda: clock[0], feed_refresh=0.3,
                    status_poll=0.1)

    async def later() -> None:
        await asyncio.sleep(0.05)
        api_server.data["action_items"][2]["status"] = "done"
        clock[0] = NOW + 301
        await asyncio.sleep(0.6)
        t5.flags &= ~q.ST_USER_ACTIVE
        t5.notify_status()

    async def go() -> s.Report:
        t5.reconnect()
        rep, _ = await asyncio.gather(sync.run(t5), later())
        return rep

    rep = asyncio.run(go())
    assert rep.outcome == s.DONE, rep.message
    assert rep.feed_pushed[0] == int(NOW) and int(NOW) + 301 in rep.feed_pushed
    assert [x["id"] for x in t5.feed_json()["actions"]] == [1, 3]


def test_pda_subcommand_is_registered(monkeypatch) -> None:
    import ble_relay
    import pda_relay

    seen: list[list[str]] = []
    monkeypatch.setattr(pda_relay, "main", lambda argv: seen.append(argv) or 0)
    assert ble_relay.main(["pda", "--once"]) == 0 and seen == [["--once"]]
