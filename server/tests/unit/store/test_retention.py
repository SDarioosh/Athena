"""Retention (CONVO_TRANSCRIPT_RETENTION_DAYS): off by default; when set, old transcripts
and kept audio go, summaries, facts and action items stay. Fictional data."""

from __future__ import annotations

from athena.links import render_conversation
from athena.mcp_server import build_mcp
from athena.pipeline import Worker
from tests.unit.mcp.test_mcp import call, process_two_speakers
from tests.unit.store.test_confirmed_facts import DAY, T0
from tests.unit.store.test_erase_fact import conversation, erase, rewriter


def retention(pipeline, days: int):
    pipeline.settings = pipeline.settings.model_copy(update={"transcript_retention_days": days})


def test_off_by_default_keeps_everything(pipeline):
    conv, _ = conversation(pipeline.store, "c0", T0)
    assert pipeline.settings.transcript_retention_days == 0
    assert pipeline.prune_transcripts(now=T0 + 3650 * DAY) is None
    assert len(pipeline.store.conversation_segments(conv)) == 3


def test_old_transcripts_go_and_the_knowledge_stays(pipeline, tmp_path):
    store = pipeline.store
    audio = tmp_path / "old.wav"
    audio.write_bytes(b"RIFF")
    old, fid = conversation(store, "c0", T0, audio_path=str(audio))
    store._conn.execute("UPDATE sessions SET status = 'done' WHERE id = 'c0'")
    new, _ = conversation(store, "c1", T0 + 40 * DAY, fact="Bob plays the cello")
    retention(pipeline, 30)
    out = pipeline.prune_transcripts(now=T0 + 45 * DAY)
    assert (out["conversations"], out["lines"], len(out["audio"])) == (1, 3, 1)
    assert not audio.exists()
    assert store.conversation_segments(old) == []
    assert len(store.conversation_segments(new)) == 3
    assert store.search_segments(query="taqueria")  # the newer one only
    assert all(h["conversation_id"] == new for h in store.fts_search("taqueria"))
    c = store.get_conversation(old)
    assert c["summary"].startswith("They planned lunch") and c["transcript_pruned_at"]
    mcp = build_mcp(store, pipeline.settings)
    assert "vegetarian" in call(mcp, "list_facts")  # facts and their links stay
    assert "removed on" in call(mcp, "get_transcript", conversation_id=old)
    page = render_conversation(pipeline.settings, c, [])
    assert "removed on" in page and "They planned lunch" in page
    # idempotent
    assert pipeline.prune_transcripts(now=T0 + 45 * DAY)["conversations"] == 0
    # a pruned conversation can still have a fact erased: its notes are rewritten
    erase(pipeline, fid)
    rewriter(pipeline)
    assert pipeline.rewrite_forgotten() == 1


def test_a_new_recording_keeps_pruned_conversations(pipeline):
    store = pipeline.store
    old, fid = conversation(store, "c0", T0)
    store._conn.execute(
        "INSERT INTO action_items (conversation_id, text, status, title)"
        " VALUES (?, 'Book the taqueria', 'open', 'Book the taqueria')", (old,))
    retention(pipeline, 30)
    assert pipeline.prune_transcripts(now=T0 + 45 * DAY)["conversations"] == 1
    process_two_speakers(pipeline, session="s9", start=T0 + 45 * DAY)
    c = store.get_conversation(old)
    assert c is not None and c["transcript_pruned_at"]
    assert c["summary"].startswith("They planned lunch")
    assert [f["id"] for f in c["facts"]] == [fid]
    assert [i["text"] for i in c["action_items"]] == ["Book the taqueria"]
    assert store._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2


def test_pending_and_failed_conversations_keep_their_lines(pipeline):
    store = pipeline.store
    conv, _ = conversation(store, "c0", T0)
    store._conn.execute("UPDATE conversations SET status = 'failed' WHERE id = ?", (conv,))
    retention(pipeline, 1)
    assert pipeline.prune_transcripts(now=T0 + 10 * DAY)["conversations"] == 0
    assert len(store.conversation_segments(conv)) == 3


def test_the_worker_prunes_at_most_hourly(pipeline):
    conversation(pipeline.store, "c0", T0)
    retention(pipeline, 1)
    worker = Worker(pipeline)
    calls = []
    pipeline.prune_transcripts = lambda now=None: calls.append(now)
    worker._maybe_prune()
    worker._maybe_prune()
    assert len(calls) == 1
