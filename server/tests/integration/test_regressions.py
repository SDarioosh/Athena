"""Regression tests for bugs found in the 2026-10 bug hunt
(see docs/archive/bug-hunt-2026-10.md)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np

from athena import audio as audio_mod
from athena.diarize import cluster
from athena.domain import Segment
from athena.pipeline import Worker
from tests.conftest import silence, tone, wav

T0 = 1_760_000_000.0


def ingest(pipeline, seq, audio, session="s1", device="omi", start=None):
    return pipeline.ingest_chunk(device=device, session_id=session, sequence=seq,
                                 started_at=T0 + 30 * seq if start is None else start,
                                 codec="wav", data=wav(audio))


def record(pipeline, session, start, freq=200):
    ingest(pipeline, 0, np.concatenate([tone(freq, 5), silence(25)]), session=session,
           start=start)
    pipeline.end_session(session)
    pipeline.process_pending()


def test_pcm16_at_other_sample_rate_is_transcribed_at_the_right_speed(pipeline):
    """``sample_rate`` was only used at upload (VAD, duration); processing decoded the
    stored raw PCM as 16 kHz, so 8 kHz audio was transcribed at double speed and the
    speech spans no longer matched the audio."""
    sr = 8000
    t = np.arange(int(5 * sr)) / sr
    voice = (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    raw = audio_mod.float_to_pcm16(np.concatenate([voice, np.zeros(25 * sr, np.float32)]))
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0, started_at=T0,
                          codec="pcm16", data=raw, sample_rate=sr)
    seen = []
    transcribe = pipeline.transcriber.transcribe

    def spy(audio, prompt=""):
        seen.append(audio.size / audio_mod.SAMPLE_RATE)
        return transcribe(audio, prompt)

    pipeline.transcriber.transcribe = spy
    pipeline.end_session("s1")
    pipeline.process_pending()
    segs = pipeline.store.session_segments("s1")
    assert [s.text for s in segs] == ["words at 300 hz"]
    assert abs(seen[0] - 5.25) < 0.1  # 5 s of speech + 0.25 s padding, not 2.6 s


def test_merging_conversations_keeps_the_absorbed_ones_closed_action_items(pipeline):
    """A recording that bridges two conversations merges them. The absorbed one's
    action items and facts were deleted with it, so an item the user had marked done
    came back open after the merged conversation was re-summarized."""
    store = pipeline.store
    record(pipeline, "a", T0)
    record(pipeline, "c", T0 + 340)  # 335 s after a: its own conversation
    convs = sorted(store.list_conversations(), key=lambda c: c["started_at"])
    assert len(convs) == 2
    later = store.get_conversation(convs[1]["id"])
    store.set_action_item_status(later["action_items"][0]["id"], "done")

    record(pipeline, "b", T0 + 180)  # within the gap of both: one conversation now
    (merged,) = store.list_conversations()
    items = store.get_conversation(merged["id"])["action_items"]
    assert [a["status"] for a in items] == ["done"]


def _two_fact_conversations(store):
    """Conversation 'old' says Bob lives in Paris; the newer 'new' updates it to Berlin."""
    ids = {}
    for name, start, fact in (("old", T0, "Bob lives in Paris"),
                              ("new", T0 + 7200, "Bob lives in Berlin")):
        store.add_chunk(session_id=name, device="omi", sequence=0, started_at=start,
                        duration=30, codec="wav", audio_path=None, speech=[])
        store.replace_segments(name, "omi", [Segment(start, start + 5, f"{fact} now")],
                               [(start, start + 30)])
        seg_ids = [s.id for s in store.session_segments(name)]
        conv = store.save_conversation(None, "omi", seg_ids)
        (fid,) = store.save_summary(conv, {
            "title": name, "summary": fact, "key_points": [], "topics": [], "people": ["Bob"],
            "action_items": [], "facts": [{"subject": "Bob", "fact": fact, "source_at": start}]})
        ids[name] = (conv, fid)
    store.link_fact(ids["new"][1], "updates", ids["old"][1])
    return ids


def test_deleting_a_conversation_restores_the_facts_it_had_replaced(pipeline):
    """A fact replaced by a fact of a conversation that is then deleted stayed
    'superseded' by a fact that no longer exists, so list_facts never showed it."""
    store = pipeline.store
    ids = _two_fact_conversations(store)
    assert [f["fact"] for f in store.list_facts(subject="Bob")] == ["Bob lives in Berlin"]
    store.replace_segments("new", "omi", [], [(T0 + 7200, T0 + 7230)])  # nothing left
    assert store.delete_empty_conversations() == 1
    assert store.get_conversation(ids["new"][0]) is None
    assert [f["fact"] for f in store.list_facts(subject="Bob")] == ["Bob lives in Paris"]


def test_worker_redoes_a_digest_when_a_late_conversation_of_that_day_arrives(
        pipeline, monkeypatch, llm_log):
    """Relays upload store-and-forward recordings late. A digest written before a
    conversation of that day was summarized was never redone, so it missed it."""
    from athena import pipeline as pipeline_mod

    day = datetime.fromtimestamp(T0, UTC)
    yesterday = day.strftime("%Y-%m-%d")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(T0 + 86400, tz)

    monkeypatch.setattr(pipeline_mod, "datetime", Clock)
    record(pipeline, "s1", T0)
    worker = Worker(pipeline)
    worker.tick()
    first = pipeline.store.get_digest(yesterday)
    assert first is not None and len(first["conversation_ids"]) == 1

    record(pipeline, "s2", T0 + 3600)  # uploaded the next day, recorded yesterday
    worker.tick()
    assert len(pipeline.store.get_digest(yesterday)["conversation_ids"]) == 2

    def digests():
        return sum(b["response_format"]["json_schema"]["name"] == "DailyDigest"
                   for b in llm_log)

    made = digests()
    assert made == 2
    worker.tick()  # nothing new: the digest is not rebuilt again
    assert digests() == made


def test_non_finite_timestamp_is_a_400_not_a_500(client):
    for bad in ("nan", "inf", "-inf"):
        r = client.post("/v1/ingest/chunk", data={
            "device": "omi", "session_id": "s1", "sequence": "0", "timestamp": bad,
            "codec": "wav"}, files={"audio": ("a.wav", wav(silence(1)), "audio/wav")})
        assert r.status_code == 400, (bad, r.status_code, r.text)


def _cluster_reference(embeddings, threshold):
    """The original O(n^4) average-linkage loop, kept to check the fast one against."""
    n = len(embeddings)
    x = np.stack([e / np.linalg.norm(e) for e in embeddings])
    sim = x @ x.T
    clusters = [[i] for i in range(n)]
    while len(clusters) > 1:
        best, pair = -2.0, (0, 0)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                s = float(sim[np.ix_(clusters[a], clusters[b])].mean())
                if s > best:
                    best, pair = s, (a, b)
        if best < threshold:
            break
        a, b = pair
        clusters[a].extend(clusters.pop(b))
    labels = [0] * n
    for cid, members in enumerate(sorted(clusters, key=min)):
        for m in members:
            labels[m] = cid
    return labels


def test_cluster_matches_average_linkage_reference():
    rng = np.random.default_rng(7)
    for trial in range(20):
        centers = rng.normal(size=(int(rng.integers(1, 6)), 32))
        n = int(rng.integers(1, 40))
        embs = centers[rng.integers(0, len(centers), n)] + rng.normal(scale=0.6, size=(n, 32))
        for threshold in (0.2, 0.45, 0.7):
            assert cluster(embs, threshold) == _cluster_reference(embs, threshold), trial


def test_cluster_is_fast_enough_for_a_long_recording():
    """A 4 h session has well over a thousand transcript pieces; the old loop was
    O(n^4) in Python and took minutes for a few hundred."""
    rng = np.random.default_rng(1)
    centers = rng.normal(size=(4, 192))
    embs = centers[rng.integers(0, 4, 1500)] + rng.normal(scale=0.5, size=(1500, 192))
    start = time.perf_counter()
    labels = cluster(embs, 0.45)
    assert time.perf_counter() - start < 30
    assert len(set(labels)) == 4
