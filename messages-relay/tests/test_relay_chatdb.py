from __future__ import annotations

import pytest

from athena_messages_relay.chatdb import ChatDB, apple_time

from conftest import T0, ChatDBBuilder, apple, ts_blob

MIA = "+15550100100"
CHAT = "iMessage;-;" + MIA


def test_apple_times():
    assert apple_time(apple(T0)) == pytest.approx(T0, abs=1e-3)
    assert apple_time(apple(T0, ns=False)) == pytest.approx(T0)
    assert apple_time(0) is None
    assert apple_time(None) is None


@pytest.mark.parametrize("schema", ["catalina", "ventura"])
def test_reads_both_schemas(tmp_path, schema):
    b = ChatDBBuilder(tmp_path / "chat.db", schema)
    b.chat(CHAT, [MIA])
    b.chat("iMessage;+;chat900", [MIA, "leo@example.com"], name="Weekend crew", group=True)
    r1, _ = b.message(CHAT, "Dinner at 7?", handle=MIA, at=T0)
    b.message(CHAT, None, body=ts_blob("Sure, see you there"), from_me=True, at=T0 + 60)
    b.message(CHAT, None, handle=MIA, assoc=2000, at=T0 + 70)  # a tapback
    b.message("iMessage;+;chat900", None, handle=MIA, item=2, at=T0 + 80)  # renamed
    b.message(CHAT, None, handle=MIA, attachments=(("IMG_0042.HEIC", "image/heic"),),
              at=T0 + 90)
    db = ChatDB(str(b.path))
    assert db.has_edits == (schema == "ventura")
    chats = db.chats()
    assert {c.guid: (c.group, c.name, c.handles) for c in chats.values()} == {
        CHAT: (False, None, [MIA]),
        "iMessage;+;chat900": (True, "Weekend crew", [MIA, "leo@example.com"])}
    rows = db.messages_after(0, 100)
    assert [r.rowid for r in rows] == [1, 2, 3, 4, 5]
    first, mine, tap, event, photo = rows
    assert (first.text, first.handle, first.from_me) == ("Dinner at 7?", MIA, False)
    assert first.at == pytest.approx(T0, abs=1e-3)
    assert (mine.text, mine.handle, mine.from_me) == ("Sure, see you there", None, True)
    assert tap.meta and event.meta and not first.meta
    assert photo.text is None
    assert photo.attachments == [{"name": "IMG_0042.HEIC", "type": "image/heic"}]
    assert db.messages_after(r1, 2)[0].rowid == 2
    assert db.max_rowid() == 5
    assert db.chat_stats()[1][0] == 4  # the tapback counts; the group event is the group's


def test_edits_unsends_and_replies(tmp_path):
    b = ChatDBBuilder(tmp_path / "chat.db", "ventura")
    b.chat(CHAT, [MIA])
    r1, g1 = b.message(CHAT, "Dinner at 7?", handle=MIA, at=T0)
    r2, _ = b.message(CHAT, "Yes!", from_me=True, at=T0 + 60, reply_to=g1)
    b.edit(r1, "Dinner at 8?", T0 + 120)
    b.unsend(r2, T0 + 180)
    db = ChatDB(str(b.path))
    changed = db.changed_since(0, upto=2, limit=10)
    assert [m.rowid for m in changed] == [1, 2]
    assert changed[0].text == "Dinner at 8?"
    assert changed[0].edited_at == pytest.approx(T0 + 120, abs=1e-3)
    assert changed[1].deleted and changed[1].reply_to == g1
    assert db.changed_since(changed[0].raw_edit, upto=2, limit=10)[0].rowid == 2
    assert db.changed_since(db.edit_max(), upto=2, limit=10) == []


def test_object_replacement_characters_are_dropped(tmp_path):
    b = ChatDBBuilder(tmp_path / "chat.db")
    b.chat(CHAT, [MIA])
    b.message(CHAT, "￼Look at this", handle=MIA,
              attachments=(("IMG_1.JPG", "image/jpeg"),))
    (m,) = ChatDB(str(b.path)).messages_after(0, 10)
    assert m.text == "Look at this"


def test_undecodable_text_is_flagged(tmp_path):
    b = ChatDBBuilder(tmp_path / "chat.db")
    b.chat(CHAT, [MIA])
    b.message(CHAT, None, body=b"garbage", handle=MIA)
    (m,) = ChatDB(str(b.path)).messages_after(0, 10)
    assert m.text is None and m.undecoded
