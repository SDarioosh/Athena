"""Storing text messages: receipts, lines, threads, edits, unsends, names
(texts/repo.py through Pipeline.ingest_messages)."""

from __future__ import annotations

import pytest

from athena.domain import ERASED_TEXT, TEXT_LINE

from .helpers import GROUP, LEO, MIA, MIA_CHAT, T0, H, lines, msg, thread_ids, upload


def test_messages_become_one_thread_with_names_and_owner_lines(pipeline):
    out = pipeline.ingest_messages(upload(
        msg("g1", T0, "Dinner at 7?"),
        msg("g2", T0 + 60, "Yes, see you there", from_me=True)))
    assert out["messages"]["added"] == 2 and out["threads"]["created"] == 1
    [conv] = thread_ids(pipeline.store)
    assert lines(pipeline.store, conv) == [("Mia Chen", "Dinner at 7?"),
                                           ("Me", "Yes, see you there")]
    segs = pipeline.store.conversation_segments(conv)
    assert [s.kind for s in segs] == [TEXT_LINE, TEXT_LINE] and segs[1].is_owner
    c = pipeline.store.get_conversation(conv)
    assert c["device"] == "imessage" and c["status"] == "pending"
    assert c["thread"] == {"source": "imessage", "chat": MIA_CHAT, "name": None,
                           "group": False, "people": ["Mia Chen"]}


def test_a_resend_changes_nothing(pipeline):
    up = upload(msg("g1", T0, "Dinner at 7?"))
    pipeline.ingest_messages(up)
    before = pipeline.store.get_conversation(thread_ids(pipeline.store)[0])
    out = pipeline.ingest_messages(up)
    assert out["messages"] == {"added": 0, "updated": 0, "unchanged": 1, "deleted": 0,
                               "skipped": 0}
    assert out["threads"] == {"changed": 0, "created": 0, "removed": 0}
    after = pipeline.store.get_conversation(thread_ids(pipeline.store)[0])
    assert after["updated_at"] == before["updated_at"]


def test_a_gap_starts_a_new_thread_and_a_backfill_merges_them(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "Morning"), msg("g3", T0 + 8 * H, "Night")))
    first, second = thread_ids(pipeline.store)
    pipeline.store.save_summary(second, {"title": "Night", "summary": "s", "key_points": [],
                                         "action_items": [{"text": "Call Mia", "owner": "Me",
                                                           "at_seconds": 0}],
                                         "people": [], "topics": [], "facts": []})
    # Messages in iCloud brings an older message late: it bridges the 8 h gap
    out = pipeline.ingest_messages(upload(msg("g2", T0 + 4 * H, "Lunch?")))
    assert out["threads"] == {"changed": 1, "created": 0, "removed": 1}
    [only] = thread_ids(pipeline.store)
    assert only == first  # the earlier thread keeps its id
    assert [t for _, t in lines(pipeline.store, only)] == ["Morning", "Lunch?", "Night"]
    # the merged thread's to-do moved with it, and the thread is summarized again
    assert [a["text"] for a in pipeline.store.get_conversation(only)["action_items"]] \
        == ["Call Mia"]
    assert pipeline.store.get_conversation(only)["status"] == "pending"


def test_an_edit_updates_the_line_and_an_unsend_removes_it_for_good(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "Dinner at 7?"), msg("g2", T0 + 5, "ok")))
    out = pipeline.ingest_messages(upload(msg("g1", T0, "Dinner at 8?", edited_at=T0 + 30)))
    assert out["messages"]["updated"] == 1
    conv = thread_ids(pipeline.store)[0]
    assert lines(pipeline.store, conv)[0] == ("Mia Chen", "Dinner at 8?")
    out = pipeline.ingest_messages(upload(msg("g2", T0 + 5, "", deleted=True)))
    assert out["messages"]["deleted"] == 1
    assert [t for _, t in lines(pipeline.store, conv)] == ["Dinner at 8?"]
    # a retry of the unsent message can't bring it back
    pipeline.ingest_messages(upload(msg("g2", T0 + 5, "ok")))
    assert [t for _, t in lines(pipeline.store, conv)] == ["Dinner at 8?"]


def test_unsending_every_message_removes_the_thread(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "oops")))
    out = pipeline.ingest_messages(upload(msg("g1", T0, "", deleted=True)))
    assert out["threads"]["removed"] == 1 and thread_ids(pipeline.store) == []


def test_attachments_read_as_words_and_empty_messages_are_skipped(pipeline):
    out = pipeline.ingest_messages(upload(
        msg("g1", T0, "", attachments=[{"name": "IMG_1.HEIC", "type": "image/heic"}]),
        msg("g2", T0 + 1, "", attachments=[{"name": "plan.pdf", "type": None}]),
        msg("g3", T0 + 2, "")))
    assert out["messages"]["added"] == 2 and out["messages"]["skipped"] == 1
    assert [t for _, t in lines(pipeline.store, thread_ids(pipeline.store)[0])] \
        == ["[photo]", "[file: plan.pdf]"]


def test_a_handle_without_a_contact_shows_its_number_until_it_gets_a_name(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "Hi, it's Leo", handle=LEO, chat="c-leo"),
                                    names={}, chats=[{"id": "c-leo", "handles": [LEO]}]))
    conv = thread_ids(pipeline.store)[0]
    assert lines(pipeline.store, conv) == [(LEO, "Hi, it's Leo")]
    pipeline.ingest_messages({"source": "imessage", "messages": [],
                              "handles": [{"handle": LEO, "name": "Leo Park"}]})
    assert lines(pipeline.store, conv) == [("Leo Park", "Hi, it's Leo")]
    assert pipeline.store.get_conversation(conv)["thread"]["people"] == ["Leo Park"]


def test_a_renamed_contact_renames_the_person_everywhere_but_the_owner_wins(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "hey")))
    pid = pipeline.store.person_id("Mia Chen")
    pipeline.ingest_messages(upload(names={MIA: "Mia Chen-Ruiz"}))
    conv = thread_ids(pipeline.store)[0]
    assert lines(pipeline.store, conv) == [("Mia Chen-Ruiz", "hey")]
    assert pipeline.store.person_id("Mia Chen-Ruiz") == pid  # same person, same id
    # the owner names her in Athena: a later contact rename doesn't undo it
    pipeline.store.rename_everywhere("Mia Chen-Ruiz", "Mia")
    pipeline.ingest_messages(upload(names={MIA: "Mia C."}))
    assert lines(pipeline.store, conv) == [("Mia", "hey")]
    assert pipeline.store.person_handles() == {pid: [{"source": "imessage", "handle": MIA}]}


def test_a_texter_and_a_recorded_speaker_with_one_name_are_one_person(pipeline):
    pid = pipeline.store.ensure_person("Mia Chen")  # heard in a recording
    pipeline.ingest_messages(upload(msg("g1", T0, "hey")))
    assert pipeline.store.person_handles() == {pid: [{"source": "imessage", "handle": MIA}]}


def test_group_chats_are_labelled_and_their_senders_named(pipeline):
    up = upload(msg("g1", T0, "Who's in for Saturday?", chat=GROUP),
                msg("g2", T0 + 9, "Me!", chat=GROUP, handle=LEO),
                names={MIA: "Mia Chen", LEO: "Leo Park"},
                chats=[{"id": GROUP, "name": "Weekend crew", "group": True,
                        "handles": [MIA, LEO]}])
    pipeline.ingest_messages(up)
    conv = thread_ids(pipeline.store)[0]
    t = pipeline.store.get_conversation(conv)["thread"]
    assert t["group"] and t["name"] == "Weekend crew"
    assert t["people"] == ["Leo Park", "Mia Chen"]
    assert lines(pipeline.store, conv) == [("Mia Chen", "Who's in for Saturday?"),
                                           ("Leo Park", "Me!")]


def test_a_message_saying_a_forgotten_fact_is_stored_forgotten(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "My door code is 4417 by the way")))
    conv = thread_ids(pipeline.store)[0]
    pipeline.store.save_summary(conv, {
        "title": "Code", "summary": "s", "key_points": [], "action_items": [], "people": [],
        "topics": [], "facts": [{"subject": "Mia Chen", "fact": "Mia's door code is 4417",
                                 "at_seconds": 0}]})
    fact = pipeline.store.get_conversation(conv)["facts"][0]
    assert pipeline.store.erase_fact(fact["id"], "private") not in (None, "already")
    assert lines(pipeline.store, conv) == [("Mia Chen", ERASED_TEXT)]
    # an edit of the erased message can't bring the words back
    pipeline.ingest_messages(upload(msg("g1", T0, "My door code is 4417!", edited_at=T0 + 1)))
    assert lines(pipeline.store, conv) == [("Mia Chen", ERASED_TEXT)]
    # nor can another message saying it
    pipeline.ingest_messages(upload(msg("g2", T0 + 60, "Mia's door code is 4417")))
    assert lines(pipeline.store, conv)[1] == ("Mia Chen", ERASED_TEXT)


def test_deleting_a_chat_removes_its_threads_and_messages(pipeline):
    pipeline.ingest_messages(upload(msg("g1", T0, "a"), msg("g2", T0 + 10 * H, "b")))
    assert len(thread_ids(pipeline.store)) == 2
    assert pipeline.store.delete_text_chat("imessage", MIA_CHAT) == {"threads": 2,
                                                                     "messages": 2}
    assert thread_ids(pipeline.store) == []
    assert pipeline.store.delete_text_chat("imessage", MIA_CHAT) is None
    # the chat can be sent again later (the owner included it again)
    pipeline.ingest_messages(upload(msg("g1", T0, "a")))
    assert len(thread_ids(pipeline.store)) == 1


@pytest.mark.parametrize("bad, error", [
    ({"source": "telegram", "messages": []}, "source must be one of"),
    ({"source": "imessage", "messages": [msg("g1", T0)]}, "messages[0].chat"),
    ({"source": "imessage", "messages": [msg("g 1", T0)]}, "messages[0].id"),
    ({"source": "imessage", "messages": [msg("g1", "noon")]}, "messages[0].at"),
    ({"source": "imessage", "messages": [msg("g1", T0, handle=None)]}, "handle is needed"),
    ({"source": "imessage", "messages": [msg("g1", T0, "x" * 4001)]}, "longer than 4000"),
])
def test_a_bad_upload_names_the_field_and_stores_nothing(pipeline, bad, error):
    with pytest.raises(ValueError, match=error.replace("[", r"\[").replace("]", r"\]")):
        pipeline.ingest_messages(bad)
    assert pipeline.store.text_stats()["messages"] == 0


def test_a_large_history_threads_in_batches_like_in_one_upload(pipeline, settings):
    from athena.db.store import Store
    from athena.pipeline import Pipeline

    times = [T0 + i * 7 * 60 + (i // 40) * 9 * H for i in range(400)]  # 10 bursts
    msgs = [msg(f"g{i}", t, f"line {i}", from_me=i % 3 == 0) for i, t in enumerate(times)]
    for i in range(0, 400, 37):  # odd batch sizes, each batch newest message first
        pipeline.ingest_messages(upload(*msgs[i:i + 37][::-1]))
    other = Pipeline(settings.model_copy(update={"data_dir": settings.data_dir / "b"}),
                     Store(settings.data_dir / "b" / "athena.db"), pipeline.vad)
    other.ingest_messages(upload(*msgs[:200]))
    other.ingest_messages(upload(*msgs[200:]))

    def shape(p):
        return [[t for _, t in lines(p.store, c)] for c in thread_ids(p.store)]
    assert shape(pipeline) == shape(other) and len(shape(other)) == 10
