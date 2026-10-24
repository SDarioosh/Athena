"""The relay algorithm (§6) against the fake watch and the fake server."""

from __future__ import annotations

import asyncio
import json
import logging
import socket

import pytest
from barvis_relay import drain as d
from barvis_relay import protocol as p
from barvis_relay.anchors import Anchors
from barvis_relay.uploader import AUTH, RETRY, SKIP, Uploader, UploadError, classify
from conftest import T0_MS, TOKEN, FakeChunk, FakeWatch, make_session

NOW = T0_MS / 1000 + 3600  # relay clock: an hour after the recordings


async def _no_sleep(_s: float) -> None:
    return None


def relay_for(server, *, token: str = TOKEN, url: str | None = None, mode: str = d.UPLOAD,
              anchors: Anchors | None = None, http_timeout: float = 3.0, save_dir=None,
              clock=lambda: NOW) -> d.Relay:
    up = Uploader(url or server.url, token, timeout=http_timeout) if mode == d.UPLOAD else None
    return d.Relay(up, anchors or Anchors(None), mode=mode, save_dir=save_dir, clock=clock,
                   timeout=2.0, sleep=_no_sleep)


def run(relay: d.Relay, watch: FakeWatch) -> d.Report:
    async def go() -> d.Report:
        watch.reconnect()
        return await relay.run(watch)

    return asyncio.run(go())


def link(watch: FakeWatch, server) -> None:
    watch.events = server.events  # one timeline for HTTP requests and ACKs


# ----------------------------------------------------------------- happy path


def test_full_drain(server) -> None:
    w = FakeWatch()
    link(w, server)
    a = make_session(w, 3, first_id=1)
    b = make_session(w, 2, first_id=4, start_ms=T0_MS + 600_000)
    rep = run(relay_for(server), w)

    assert rep.outcome == d.DONE, rep.message
    assert rep.acked == [1, 2, 3, 4, 5] and not w.chunks
    ops = w.opcodes()
    assert ops[0] == p.OP_TIME and ops[1] == p.OP_LIST and ops[-1] == p.OP_BYE
    assert int.from_bytes(w.commands[0][4:12], "little") == int(NOW * 1000)

    ingests = server.ingests()
    assert len(ingests) == 5
    for req, c in zip(ingests, a + b, strict=True):
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        assert req.fields == {"device": "twatch-ultra", "session_id": c.session_id.hex(),
                              "sequence": str(c.sequence), "timestamp": p.iso_ms(c.start_ms),
                              "codec": "ogg"}
        fname, ctype, data = req.files["audio"]
        assert (fname, ctype, data) == (f"{c.session_id.hex()}-{c.sequence}.opus",
                                        "audio/ogg", c.data)
    assert ingests[1].fields["timestamp"] == "2026-10-19T12:00:30.000Z"

    # /end of each session comes after its last chunk's upload and before that chunk's ACK
    ends = [f"/v1/sessions/{s[0].session_id.hex()}/end" for s in (a, b)]
    ev = server.events
    assert ev.index(("http", ends[0])) == ev.index(("ack", 3)) - 1
    assert ev.index(("http", ends[1])) == ev.index(("ack", 5)) - 1
    # every ACK follows its chunk's upload
    assert [e for e in ev if e[0] == "ack"] == [("ack", i) for i in range(1, 6)]
    assert server.paths()[0] == "/health"


def test_note_session_uploads_with_kind_note(server) -> None:
    w = FakeWatch()
    link(w, server)
    conv = make_session(w, 1, first_id=1)
    note = make_session(w, 2, first_id=2, start_ms=T0_MS + 60_000, flags=p.FLAG_NOTE)
    rep = run(relay_for(server), w)

    assert rep.outcome == d.DONE, rep.message
    assert rep.acked == [1, 2, 3] and not w.chunks
    ingests = server.ingests()
    assert "kind" not in ingests[0].fields  # ordinary chunk: field omitted
    for req, c in zip(ingests[1:], note, strict=True):
        assert req.fields == {"device": "twatch-ultra", "session_id": c.session_id.hex(),
                              "sequence": str(c.sequence), "timestamp": p.iso_ms(c.start_ms),
                              "codec": "ogg", "kind": "note"}
    assert f"/v1/sessions/{note[0].session_id.hex()}/end" in server.paths()
    assert f"/v1/sessions/{conv[0].session_id.hex()}/end" in server.paths()


def test_list_paging(server) -> None:
    w = FakeWatch()
    make_session(w, 70, size=200)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and len(rep.acked) == 70
    lists = [int.from_bytes(c[1:5], "little") for c in w.commands if c[0] == p.OP_LIST]
    assert lists == [0, 32, 64, 70]  # continue after the last id until empty + remaining 0


def test_small_mtu_frames_and_multi_frame_chunks(server) -> None:
    w = FakeWatch(mtu=64)  # 52-byte DATA payloads
    make_session(w, 2, size=5000)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1, 2]


def test_mtu_517_frames_fit_512(server) -> None:
    w = FakeWatch(mtu=517)  # 503-byte DATA payloads, 512-byte notifications
    chunks = make_session(w, 1, size=5000)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1]
    assert server.ingests()[0].files["audio"][2] == chunks[0].data


def test_duplicate_counts_as_ok(server) -> None:
    w = FakeWatch()
    make_session(w, 2)
    server.plan = [("dup",), ("dup",)]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1, 2] and rep.duplicates == [1, 2]


def test_lost_ack_reuploads_as_duplicate(server) -> None:
    w = FakeWatch()
    make_session(w, 2)
    w.drop_on_ack = {1}
    relay = relay_for(server)
    rep = run(relay, w)
    assert rep.outcome == d.LINK_LOST and not w.acked
    server.plan = [("dup",)]
    rep = run(relay, w)
    assert rep.outcome == d.DONE and rep.acked == [1, 2]
    assert [r.fields["sequence"] for r in server.ingests()] == ["0", "0", "1"]


def test_busy_is_retried(server) -> None:
    w = FakeWatch(busy_next=2)
    make_session(w, 1)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1]


# ----------------------------------------------------------------- server errors


def test_server_unreachable_pulls_nothing() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed after this block: connection refused
    w = FakeWatch()
    make_session(w, 2)
    rep = run(relay_for(None, url=f"http://127.0.0.1:{port}"), w)
    assert rep.outcome == d.SERVER_RETRY
    assert w.opcodes() == [p.OP_TIME, p.OP_BYE]  # TIME still sent, no LIST/READ/ACK
    assert len(w.chunks) == 2


def test_health_5xx_pulls_nothing(server) -> None:
    server.health_status = 503
    w = FakeWatch()
    make_session(w, 1)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.SERVER_RETRY and w.opcodes() == [p.OP_TIME, p.OP_BYE]


@pytest.mark.parametrize("status", [500, 502, 408, 429])
def test_retryable_error_stops_without_ack(server, status: int) -> None:
    w = FakeWatch()
    make_session(w, 3)
    server.plan = [("ok",), (status, "try later")]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.SERVER_RETRY
    assert rep.acked == [1] and sorted(w.chunks) == [2, 3]
    assert [r[0] for r in w.reads] == [1, 2]  # stopped draining
    assert w.opcodes()[-1] == p.OP_BYE


def test_timeout_stops_without_ack(server) -> None:
    w = FakeWatch()
    make_session(w, 2)
    server.plan = [("timeout", 2.0)]
    rep = run(relay_for(server, http_timeout=0.5), w)
    assert rep.outcome == d.SERVER_RETRY and not rep.acked and len(w.chunks) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_stops(server, status: int) -> None:
    w = FakeWatch()
    make_session(w, 2)
    server.plan = [(status, "no")]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.AUTH_FAILED and "token rejected" in rep.message
    assert not rep.acked and len(w.chunks) == 2 and len(server.ingests()) == 1


def test_wrong_token_is_auth_error(server) -> None:
    w = FakeWatch()
    make_session(w, 1)
    rep = run(relay_for(server, token="wrong-fictional-token"), w)
    assert rep.outcome == d.AUTH_FAILED and len(w.chunks) == 1


@pytest.mark.parametrize("status", [400, 413, 422])
def test_client_error_skips_chunk_and_continues(server, status: int) -> None:
    w = FakeWatch()
    make_session(w, 3)
    server.plan = [(status, "could not decode audio")]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE
    assert rep.acked == [2, 3] and list(w.chunks) == [1]
    assert "could not decode audio" in rep.skipped[1]


def test_end_failure_keeps_last_chunk(server) -> None:
    w = FakeWatch()
    make_session(w, 2)
    server.end_plan = [(503, "busy")]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.SERVER_RETRY and rep.acked == [1] and list(w.chunks) == [2]
    rep = run(relay_for(server), w)  # next connection: re-upload (dedupe server-side), end, ack
    assert rep.outcome == d.DONE and rep.acked == [2] and not w.chunks


def test_redirect_is_not_followed(server) -> None:
    w = FakeWatch()
    make_session(w, 1)
    server.plan = [("redirect", server.url + "/elsewhere")]
    rep = run(relay_for(server), w)
    assert rep.outcome == d.SERVER_RETRY and not rep.acked
    assert "/elsewhere" not in server.paths()


# ----------------------------------------------------------------- watch side


def test_crc_mismatch_rereads_then_succeeds(server) -> None:
    w = FakeWatch(corrupt_reads={2: 2})
    c = make_session(w, 3)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1, 2, 3]
    assert [r for r in w.reads if r[0] == 2] == [(2, 0)] * 3
    assert server.ingests()[1].files["audio"][2] == c[1].data


def test_crc_mismatch_three_rereads_then_skip(server) -> None:
    w = FakeWatch(corrupt_reads={2: 99})
    make_session(w, 3)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [1, 3] and list(w.chunks) == [2]
    assert len([r for r in w.reads if r[0] == 2]) == 1 + d.MAX_REREADS
    assert "CRC-32" in rep.skipped[2]
    assert len(server.ingests()) == 2  # a bad chunk is never uploaded


def test_resume_read_after_disconnect(server) -> None:
    w = FakeWatch()
    chunks = make_session(w, 2, size=3000)
    w.drop_after_frames = {1: 3}
    relay = relay_for(server)
    rep = run(relay, w)
    assert rep.outcome == d.LINK_LOST and not w.acked
    step = p.data_payload_max(w.mtu)
    rep = run(relay, w)
    assert rep.outcome == d.DONE and rep.acked == [1, 2]
    assert w.reads[:2] == [(1, 0), (1, 3 * step)]  # resumed at the bytes already received
    assert server.ingests()[0].files["audio"][2] == chunks[0].data


def test_resume_with_changed_entry_starts_over(server) -> None:
    w = FakeWatch()
    c = make_session(w, 1, size=3000)[0]
    w.drop_after_frames = {1: 2}
    relay = relay_for(server)
    run(relay, w)
    c.data = c.data[:-1] + b"X"  # different file under the same id: crc differs
    rep = run(relay, w)
    assert rep.outcome == d.DONE and w.reads[-1] == (1, 0)


def test_not_found_skips(server) -> None:
    w = FakeWatch(vanish={1})
    make_session(w, 2)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.DONE and rep.acked == [2] and 1 in rep.skipped


def test_read_io_error_retries_later(server) -> None:
    w = FakeWatch(read_io_error={2})
    make_session(w, 3)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.WATCH_RETRY and rep.acked == [1]
    assert w.opcodes()[-1] == p.OP_BYE


def test_mtu_too_small(server) -> None:
    w = FakeWatch(mtu=23)
    make_session(w, 1)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.WATCH_RETRY and "MTU is 23" in rep.message


def test_incompatible_proto(server) -> None:
    w = FakeWatch(info=b"proto=2;device=twatch-ultra")
    make_session(w, 1)
    rep = run(relay_for(server), w)
    assert rep.outcome == d.INCOMPATIBLE and w.opcodes() == [p.OP_BYE]


def test_receive_timeout_is_link_lost(server) -> None:
    class Mute(FakeWatch):
        def _time(self, cmd: bytes) -> None:
            pass  # never answers

    w = Mute()
    relay = relay_for(server)
    relay.timeout = 0.1
    rep = run(relay, w)
    assert rep.outcome == d.LINK_LOST and "didn't answer" in rep.message


# ----------------------------------------------------------------- time


def test_time_unset_fixed_by_time_command(server) -> None:
    w = FakeWatch()
    c = make_session(w, 2, start_ms=0, flags=p.FLAG_TIME_UNSET)
    for x in c:
        x.unset_session_start_ms = T0_MS
    rep = run(relay_for(server), w)
    assert rep.time_fixed == 2
    assert [r.fields["timestamp"] for r in server.ingests()] == [
        "2026-10-19T12:00:00.000Z", "2026-10-19T12:00:30.000Z"]


def test_time_unset_from_earlier_boot_is_anchored(server, tmp_path) -> None:
    w = FakeWatch()
    c = make_session(w, 2, start_ms=0, flags=p.FLAG_TIME_UNSET, last=False)
    for x in c:
        x.same_boot = False
    path = tmp_path / "anchors.json"
    clock = [NOW]
    relay = relay_for(server, anchors=Anchors(path, clock=lambda: clock[0]))
    w.drop_on_ack = {2}  # second chunk uploaded but not acked: comes back next time
    rep = run(relay, w)
    assert rep.time_fixed == 0
    anchor_ms = int(NOW * 1000)
    assert [r.fields["timestamp"] for r in server.ingests()] == [
        p.iso_ms(anchor_ms), p.iso_ms(anchor_ms + 30_000)]
    assert json.loads(path.read_text())[c[0].session_id.hex()]["anchor_ms"] == anchor_ms

    clock[0] = NOW + 7200  # a later connection, and a fresh relay process
    relay2 = relay_for(server, anchors=Anchors(path, clock=lambda: clock[0]))
    rep = run(relay2, w)
    assert rep.outcome == d.DONE
    assert server.ingests()[-1].fields["timestamp"] == p.iso_ms(anchor_ms + 30_000)


def test_anchors_survive_a_broken_file(tmp_path) -> None:
    path = tmp_path / "anchors.json"
    path.write_text("{not json")
    a = Anchors(path, clock=lambda: 100.0)
    assert a.anchor_ms("00") == 100_000
    assert json.loads(path.read_text())["00"]["anchor_ms"] == 100_000


# ----------------------------------------------------------------- modes


def test_list_mode_changes_nothing(server) -> None:
    w = FakeWatch()
    make_session(w, 3)
    rep = run(relay_for(server, mode=d.LIST_ONLY), w)
    assert rep.outcome == d.DONE and [e.chunk_id for e in rep.listed] == [1, 2, 3]
    assert set(w.opcodes()) == {p.OP_LIST, p.OP_BYE}  # no TIME, READ or ACK
    assert not server.requests and rep.status and rep.status.queued_chunks == 3


def test_dry_run_saves_and_keeps(server, tmp_path) -> None:
    w = FakeWatch()
    c = make_session(w, 2)
    rep = run(relay_for(server, mode=d.DRY_RUN, save_dir=tmp_path), w)
    assert rep.outcome == d.DONE and not rep.acked and len(w.chunks) == 2
    assert not server.requests and p.OP_ACK not in w.opcodes()
    assert (tmp_path / f"{c[1].session_id.hex()}-1.opus").read_bytes() == c[1].data


# ----------------------------------------------------------------- privacy


def test_token_never_logged(server, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    w = FakeWatch()
    make_session(w, 3)
    server.plan = [("ok",), (422, "bad"), (500, "boom")]
    run(relay_for(server), w)
    run(relay_for(server, token=TOKEN + "x"), w)  # rejected token
    run(relay_for(server), FakeWatch(corrupt_reads={1: 99}, chunks={1: FakeChunk(
        1, b"\x01" * 8, 0, T0_MS, b"OggS" + bytes(100))}))
    assert caplog.records
    assert TOKEN not in caplog.text
    up = Uploader(server.url, TOKEN)
    assert TOKEN not in repr(up) and TOKEN not in str(vars(d.Report()))


# ----------------------------------------------------------------- uploader unit


def test_classify() -> None:
    assert [classify(s) for s in (401, 403)] == [AUTH, AUTH]
    assert [classify(s) for s in (500, 503, 408, 429, 302)] == [RETRY] * 5
    assert [classify(s) for s in (400, 404, 413, 422)] == [SKIP] * 4


def test_uploader_rejects_bad_settings() -> None:
    for url in ("ftp://x", "http://user:pw@host", "http://h/?access_token=x", "nohost",
                # each would send the request somewhere other than <server>/v1/...
                "http://127.0.0.1:8000#foo", "http://127.0.0.1:8000?", "http://@127.0.0.1:8000",
                "http://h/#"):
        with pytest.raises(ValueError):
            Uploader(url, TOKEN)
    with pytest.raises(ValueError):
        Uploader("http://h", "a\nb")


def test_upload_answer_without_ok(server, monkeypatch) -> None:
    up = Uploader(server.url, TOKEN)
    monkeypatch.setattr(up, "_request", lambda *a, **k: {"ok": False})
    with pytest.raises(UploadError) as e:
        up.upload_chunk(device="d", session_id="s", sequence=0, timestamp="t", audio=b"")
    assert e.value.kind == RETRY


def test_upload_chunk_kind_field(server) -> None:
    up = Uploader(server.url, TOKEN)
    up.upload_chunk(device="d", session_id="s", sequence=0, timestamp="t", audio=b"x")
    up.upload_chunk(device="d", session_id="s", sequence=1, timestamp="t", audio=b"x",
                    kind="note")
    plain, note = server.ingests()
    assert "kind" not in plain.fields
    assert note.fields["kind"] == "note"


def test_backoff() -> None:
    b = d.Backoff()
    assert [b.next() for _ in range(7)] == [60, 120, 300, 600, 900, 900, 900]
    b.reset()
    assert b.next() == 60


class _BrokenHTTP:
    """A server that answers /health and then cuts every other answer short."""

    def __init__(self, broken: bytes) -> None:
        import threading

        self.broken = broken
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    got = conn.recv(65536)
                    if not got:
                        break
                    head += got
                if head.startswith(b"GET /health"):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n"
                                 b"Connection: close\r\n\r\n{\"ok\":true}")
                else:
                    conn.sendall(self.broken)

    def close(self) -> None:
        self.sock.close()


@pytest.mark.parametrize("broken", [
    # The connection closes before the announced body is complete: http.client raises
    # IncompleteRead, an HTTPException that is not an OSError.
    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{\"ok\": tr",
    # Not HTTP at all: BadStatusLine (HTTPException).
    b"SSH-2.0-OpenSSH_9.6\r\n",
])
def test_broken_http_answer_is_retry_later(broken: bytes) -> None:
    srv = _BrokenHTTP(broken)
    try:
        w = FakeWatch()
        make_session(w, 2)
        rep = run(d.Relay(Uploader(srv.url, TOKEN, timeout=3.0), Anchors(None), clock=lambda: NOW,
                          timeout=2.0, sleep=_no_sleep), w)
    finally:
        srv.close()
    # Relay.run never raises for server trouble: retry later, nothing acked, BYE sent.
    assert rep.outcome == d.SERVER_RETRY, rep.message
    assert not rep.acked and len(w.chunks) == 2
    assert w.opcodes()[-1] == p.OP_BYE
