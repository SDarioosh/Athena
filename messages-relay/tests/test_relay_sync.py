from __future__ import annotations

import pytest

from athena_messages_relay import state as state_mod
from athena_messages_relay.chatdb import ChatDB
from athena_messages_relay.config import Config
from athena_messages_relay.contacts import Contacts
from athena_messages_relay.sync import Syncer
from athena_messages_relay.upload import ServerUnavailable, Uploader, UploadError

from conftest import T0, make_contacts

MIA, LEO = "+15550100100", "leo@example.com"
CHAT = "iMessage;-;" + MIA
GROUP = "iMessage;+;chat900"


@pytest.fixture
def setup(tmp_path, chatdb, server):
    chatdb.chat(CHAT, [MIA])
    chatdb.chat(GROUP, [MIA, LEO], name="Weekend crew", group=True)
    make_contacts(tmp_path / "ab", [("Mia", "Chen", ["(555) 010-0100"], [])])

    def make(**cfg_kw):
        cfg = Config(url=server.url, token="tok-0123456789abcdef", chat_db=str(chatdb.path),
                     contacts_dir=str(tmp_path / "ab"), **cfg_kw)
        contacts = Contacts(cfg.contacts_dir)
        contacts.reload_if_changed()
        up = Uploader(cfg.url, cfg.token, retries=2, backoff=0, sleep=lambda s: None)
        return Syncer(cfg, ChatDB(cfg.chat_db), contacts, up, tmp_path / "state.json")
    return make


def test_first_pass_sends_everything_in_batches(setup, chatdb, server):
    for i in range(5):
        chatdb.message(CHAT, "message %d" % i, handle=MIA, from_me=i % 2 == 1)
    syncer = setup(batch_size=2)
    counts = syncer.run_once()
    assert (counts.sent, counts.batches) == (5, 3)
    posts = server.posts()
    assert [len(p["messages"]) for p in posts] == [2, 2, 1]
    assert all(r["auth"] == "Bearer tok-0123456789abcdef" for r in server.requests)
    first = posts[0]
    assert first["source"] == "imessage"
    assert first["chats"] == [{"id": CHAT, "name": None, "group": False, "handles": [MIA]}]
    assert first["handles"] == [{"handle": MIA, "name": "Mia Chen"}]
    m0, m1 = first["messages"]
    assert m0 == {"id": "MSG-0001", "chat": CHAT, "at": "2026-10-01T12:01:00.000Z",
                  "from_me": False, "handle": MIA, "text": "message 0", "edited_at": None,
                  "deleted": False, "reply_to": None, "attachments": []}
    assert m1["from_me"] and m1["handle"] is None
    assert posts[1]["handles"] == []  # the name was sent already
    assert state_mod.load(syncer.state_file).last_rowid == 5
    assert syncer.run_once().sent == 0  # nothing new
    assert len(server.posts()) == 3


def test_left_out_messages_still_move_the_cursor(setup, chatdb, server):
    chatdb.message(CHAT, "Your verification code is 482913", handle="55501")
    chatdb.message(CHAT, None, handle=MIA, assoc=2001)  # tapback
    chatdb.message(CHAT, None, handle=MIA)  # nothing in it
    chatdb.message(CHAT, "x" * 5000, handle=MIA)
    syncer = setup()
    counts = syncer.run_once()
    assert (counts.sent, counts.skipped) == (1, 3)
    (msg,) = server.posts()[0]["messages"]
    assert len(msg["text"]) == 4000 and msg["text"].endswith("…")
    assert syncer.state.last_rowid == 4


def test_since_and_groups(setup, chatdb, server):
    chatdb.message(CHAT, "old", handle=MIA, at=T0 - 40 * 86400)
    chatdb.message(CHAT, "new", handle=MIA, at=T0)
    chatdb.message(GROUP, "group hi", handle=LEO, at=T0 + 10)
    syncer = setup(since="2026-09-30", include_groups=False)
    syncer.run_once()
    sent = [m["text"] for p in server.posts() for m in p["messages"]]
    assert sent == ["new"]
    deletes = [r["path"] for r in server.requests if r["method"] == "DELETE"]
    assert deletes == ["/v1/ingest/messages/chats/imessage/iMessage%3B%2B%3Bchat900"]


def test_attachments_only(setup, chatdb, server):
    chatdb.message(CHAT, None, handle=MIA, attachments=(("IMG_0042.HEIC", "image/heic"),))
    setup().run_once()
    (msg,) = server.posts()[0]["messages"]
    assert msg["text"] == "" and msg["attachments"] == [
        {"name": "IMG_0042.HEIC", "type": "image/heic"}]


def test_server_errors_are_retried_and_refusals_keep_the_cursor(setup, chatdb, server):
    chatdb.message(CHAT, "hi", handle=MIA)
    server.replies = [(503, {"detail": "busy"}), (200, {"ok": True})]
    syncer = setup()
    assert syncer.run_once().sent == 1
    chatdb.message(CHAT, "again", handle=MIA)
    server.replies = [(401, {"detail": "unauthorized"})]  # a wrong token stops the pass
    with pytest.raises(UploadError, match="unauthorized"):
        syncer.run_once()
    assert state_mod.load(syncer.state_file).last_rowid == 1
    server.replies = [(500, {})] * 3
    with pytest.raises(ServerUnavailable):
        syncer.run_once()
    assert state_mod.load(syncer.state_file).last_rowid == 1
    assert syncer.run_once().sent == 1  # the same batch, once the server is back


def test_a_message_the_server_refuses_is_found_and_left_out(setup, chatdb, server):
    for i in range(4):
        chatdb.message(CHAT, "message %d" % i, handle=MIA)
    # the batch of 4 is refused, then its halves: [0, 1] ok, [2, 3] refused, 2 ok, 3 refused
    server.replies = [(400, {"detail": "messages[3].at"}), (200, {"ok": True}),
                      (400, {"detail": "messages[1].at"}), (200, {"ok": True}),
                      (400, {"detail": "messages[0].at"})]
    syncer = setup()
    counts = syncer.run_once()
    assert (counts.sent, counts.skipped) == (3, 1)
    assert [[m["id"][-1] for m in p["messages"]] for p in server.posts()] == [
        ["1", "2", "3", "4"], ["1", "2"], ["3", "4"], ["3"], ["4"]]
    assert state_mod.load(syncer.state_file).last_rowid == 4  # not stuck on message 4


def test_edits_and_unsends(setup, chatdb, server):
    r1, _ = chatdb.message(CHAT, "Dinner at 7?", handle=MIA, at=T0)
    r2, _ = chatdb.message(CHAT, "oops", from_me=True, at=T0 + 60)
    syncer = setup()
    syncer.run_once()
    n = len(server.posts())
    chatdb.edit(r1, "Dinner at 8?", T0 + 120)
    chatdb.unsend(r2, T0 + 180)
    counts = syncer.run_once()
    assert counts.changed == 2
    changed = server.posts()[n]["messages"]
    assert changed[0]["text"] == "Dinner at 8?"
    assert changed[0]["edited_at"] == "2026-10-01T12:02:00.000Z"
    assert changed[1]["deleted"] and changed[1]["text"] == "" and changed[1]["attachments"] == []
    assert syncer.run_once().changed == 0


def test_new_message_already_unsent_is_not_sent(setup, chatdb, server):
    r1, _ = chatdb.message(CHAT, "oops", from_me=True)
    chatdb.unsend(r1, T0 + 999)
    syncer = setup()
    counts = syncer.run_once()
    assert counts.sent == 0 and server.posts() == []


def test_exclude_removes_once_and_include_sends_again(setup, chatdb, server):
    chatdb.message(CHAT, "hi Mia", handle=MIA)
    chatdb.message(GROUP, "hi crew", handle=LEO)
    syncer = setup()
    syncer.run_once()
    syncer.cfg.exclude = ["mia chen"]  # her chat and the group she is in
    syncer.run_once()
    syncer.run_once()
    deletes = sorted(r["path"] for r in server.requests if r["method"] == "DELETE")
    assert len(deletes) == 2
    chatdb.message(CHAT, "not sent", handle=MIA)
    before = len(server.posts())
    syncer.run_once()
    assert len(server.posts()) == before
    syncer.cfg.exclude = []
    for chat in syncer.chats.values():
        syncer.include_again(chat)
    syncer.run_once()
    again = sorted(m["text"] for p in server.posts()[before:] for m in p["messages"])
    assert again == ["hi Mia", "hi crew", "not sent"]


def test_contact_name_changes_are_sent(setup, chatdb, server, tmp_path):
    chatdb.message(CHAT, "hi", handle=MIA)
    syncer = setup()
    syncer.run_once()
    syncer.contacts.names = {"5550100100": "Mia Chen-Park"}
    syncer.run_once()
    last = server.posts()[-1]
    assert last == {"source": "imessage", "messages": [],
                    "handles": [{"handle": MIA, "name": "Mia Chen-Park"}]}
    syncer.run_once()
    assert server.posts()[-1] == last and len(server.posts()) == 2
