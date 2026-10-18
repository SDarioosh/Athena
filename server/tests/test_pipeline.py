"""End-to-end pipeline with fake whisper/LLM/embedder: transcribe, diarize, split, summarize."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
from conftest import ToneEmbedder, fake_llm_transport, silence, tone, wav

from convo_server import store as store_mod
from convo_server.conversations import split_on_gaps
from convo_server.diarize import Diarizer, EnrolledVoice, cluster
from convo_server.llm import LLMClient, Summarizer
from convo_server.pipeline import Worker
from convo_server.search import index_conversation, search
from convo_server.store import Segment

T0 = 1_760_000_000.0


def ingest(pipeline, seq, audio, session="s1", device="omi", start=None):
    return pipeline.ingest_chunk(device=device, session_id=session, sequence=seq,
                                 started_at=T0 + 30 * seq if start is None else start,
                                 codec="wav", data=wav(audio))


def test_split_on_gaps():
    segs = [Segment(0, 5, "a"), Segment(100, 110, "b"), Segment(291, 300, "c"),
            Segment(301, 302, "d")]
    groups = split_on_gaps(segs, 180)
    assert [[s.text for s in g] for g in groups] == [["a", "b"], ["c", "d"]]


def test_cluster_groups_similar_vectors():
    e = ToneEmbedder()
    vecs = np.stack([e.embed(tone(f, 1)) for f in (200, 600, 200, 600, 1000)])
    assert cluster(vecs, 0.6) == [0, 1, 0, 1, 2]


def test_diarizer_labels_owner_and_unknowns():
    e = ToneEmbedder()
    d = Diarizer(e, [EnrolledVoice("Me", e.embed(tone(200, 2)), True),
                     EnrolledVoice("Alice", e.embed(tone(600, 2)), False)])
    labels = d.label([tone(200, 2), tone(600, 2), tone(900, 2), tone(200, 0.3)],
                     [0, 3, 6, 7])
    assert [(x.name, x.is_owner) for x in labels] == [
        ("Me", True), ("Alice", False), ("Speaker 1", False), ("Speaker 1", False)]


def test_full_pipeline(pipeline, llm_log):
    # minute 0: owner (200 Hz) then other person (600 Hz); 5 min silence; then more talk
    ingest(pipeline, 0, np.concatenate([tone(200, 4), silence(4), tone(600, 4), silence(18)]))
    for seq in range(1, 11):
        ingest(pipeline, seq, silence(30))
    ingest(pipeline, 11, np.concatenate([silence(5), tone(600, 6), silence(19)]))
    assert pipeline.end_session("s1")
    audio_dir = pipeline.settings.audio_dir / "omi" / "s1"
    assert len(list(audio_dir.iterdir())) == 12

    assert pipeline.process_pending() == 1
    store = pipeline.store
    assert store.get_session("s1")["status"] == "done"
    # raw audio deleted after transcription by default
    assert not audio_dir.exists()
    assert all(c["audio_path"] is None for c in store.chunks("s1"))

    convs = sorted(store.list_conversations(), key=lambda c: c["started_at"])
    assert len(convs) == 2, "gap of >3 min must split conversations"
    first = store.get_conversation(convs[0]["id"])
    segs = store.conversation_segments(first["id"])
    assert [(s.speaker, s.is_owner) for s in segs] == [("Me", True), ("Speaker 1", False)]
    assert segs[0].text == "words at 200 hz"
    assert abs(segs[0].started_at - (T0 - 0.25)) < 0.5  # absolute wall-clock time

    assert first["status"] == "summarized" and first["title"] == "Planning the trip"
    assert first["action_items"][0]["text"] == "Book flights"
    assert first["action_items"][0]["source_at"] == first["started_at"] + 1
    assert first["facts"][0]["fact"] == "Alice is vegetarian"

    # the LLM got a timestamped, speaker-labelled transcript and a JSON schema
    req = llm_log[0]
    assert req["response_format"]["type"] == "json_schema"
    assert "[0] Me: words at 200 hz" in req["messages"][1]["content"]

    hits = search(store, "vegetarian")
    assert hits and hits[0]["conversation_id"] in {c["id"] for c in convs}


def test_late_chunk_merges_into_existing_conversation(pipeline):
    ingest(pipeline, 0, np.concatenate([tone(200, 5), silence(25)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert len(pipeline.store.list_conversations()) == 1
    # new session 60 s later on the same device: within the 3 min gap -> same conversation
    ingest(pipeline, 0, np.concatenate([tone(600, 5), silence(25)]), session="s2",
           start=T0 + 60)
    pipeline.end_session("s2")
    pipeline.process_pending()
    convs = pipeline.store.list_conversations()
    assert len(convs) == 1
    assert len(pipeline.store.conversation_segments(convs[0]["id"])) == 2


def test_silent_session_produces_nothing(pipeline):
    ingest(pipeline, 0, silence(30))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.store.get_session("s1")["status"] == "done"
    assert pipeline.store.list_conversations() == []


def test_transcription_failure_keeps_audio(pipeline):
    pipeline.transcriber.fail = True
    ingest(pipeline, 0, np.concatenate([tone(200, 5), silence(25)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    sess = pipeline.store.get_session("s1")
    assert sess["status"] == "failed" and "whisper down" in sess["error"]
    assert all(Path(c["audio_path"]).exists() for c in pipeline.store.chunks("s1"))


def test_idle_sessions_are_finalized(pipeline):
    ingest(pipeline, 0, silence(30))
    assert pipeline.finalize_idle(now=10**12) == ["s1"]
    assert pipeline.store.get_session("s1")["stop_reason"] == "idle"


def test_embeddings_and_hybrid_search(pipeline):
    class BagEmbedder:
        model = "bag"
        vocab = ["lisbon", "trip", "vegetarian", "flights", "alice", "book"]

        def embed(self, texts):
            return np.array([[t.lower().count(w) + 0.01 for w in self.vocab] for t in texts],
                            dtype=np.float32)

    ingest(pipeline, 0, np.concatenate([tone(200, 5), silence(25)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    conv_id = pipeline.store.list_conversations()[0]["id"]
    assert index_conversation(pipeline.store, BagEmbedder(), conv_id) == 3
    hits = search(pipeline.store, "dietary needs vegetarian", embedder=BagEmbedder())
    assert hits[0]["conversation_id"] == conv_id
    kinds = {m["kind"] for m in hits[0]["matches"]}
    assert "fact" in kinds


def test_digest(pipeline):
    ingest(pipeline, 0, np.concatenate([tone(200, 5), silence(25)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    from datetime import UTC, datetime

    date = datetime.fromtimestamp(T0, UTC).strftime("%Y-%m-%d")
    digest = pipeline.make_digest(date)
    assert digest["headline"] == "A day of plans" and digest["date"] == date
    assert pipeline.store.get_digest(date) == digest
    assert pipeline.make_digest("2001-01-01") is None


class FlakyLLM:
    """llama-server stand-in that can be switched off; counts summary requests."""

    def __init__(self) -> None:
        self.down = False
        self.requests = 0
        self.ok = fake_llm_transport()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.down:
                raise httpx.ConnectError("connection refused", request=request)
            return self.ok.handle_request(request)

        return httpx.MockTransport(handler)


def flaky_pipeline(pipeline):
    llm = FlakyLLM()
    pipeline.summarizer = Summarizer(LLMClient("http://llm.test/v1", "test-model",
                                               transport=llm.transport()), "Me")
    return llm


def record(pipeline, session, start):
    ingest(pipeline, 0, np.concatenate([tone(200, 5), silence(25)]), session=session,
           start=start)
    pipeline.end_session(session)


def test_failed_summary_is_retried(pipeline):
    llm = flaky_pipeline(pipeline)
    llm.down = True
    record(pipeline, "s1", T0)
    pipeline.process_pending()
    conv = pipeline.store.list_conversations()[0]
    assert conv["status"] == "failed" and "unreachable" in conv["error"]
    assert conv["title"] is None

    llm.down = False  # llama-server is back; `convo-server summarize-pending` retries now
    assert pipeline.summarize_pending() == 1
    conv = pipeline.store.get_conversation(conv["id"])
    assert conv["status"] == "summarized" and conv["error"] is None
    assert conv["title"] == "Planning the trip"
    assert [a["text"] for a in conv["action_items"]] == ["Book flights"]


def test_worker_backs_off_on_failed_summaries(pipeline, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(store_mod, "time", SimpleNamespace(time=lambda: clock[0]))

    def tick(seconds_later: float) -> None:
        clock[0] += seconds_later
        worker.tick(clock[0])

    llm = flaky_pipeline(pipeline)
    llm.down = True
    record(pipeline, "s1", T0)
    record(pipeline, "s2", T0 + 3600)  # a second, separate conversation
    worker = Worker(pipeline)
    store = pipeline.store
    tick(0)
    assert len(store.conversations_with_status("failed")) == 2
    attempts = llm.requests
    assert attempts == 2  # one attempt per conversation

    # passes every 15 s inside the back-off window: no requests to the down server
    for _ in range(19):
        tick(15)
    assert llm.requests == attempts

    # after the delay, one retry; it fails, so the other conversation waits a pass
    delay = pipeline.settings.summary_retry_seconds
    tick(delay - 19 * 15)
    assert llm.requests == attempts + 1
    tick(15)
    assert llm.requests == attempts + 2  # now the other one's turn
    for _ in range(5):
        tick(15)
    assert llm.requests == attempts + 2  # both just failed again: back off

    llm.down = False
    tick(delay)
    tick(15)
    assert store.conversations_with_status("failed") == []
    assert len(store.conversations_with_status("summarized")) == 2
