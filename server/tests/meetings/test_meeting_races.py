"""The ingest/finalize races of a meeting (a session with tracks), each ordering made
deterministic: by calling the steps in order, by a hook inside the step where the race
lives (decode/VAD of an upload, whisper of a processing pass), or with two real threads
held by events. They pin what happens today.

1. ``_late_meeting_chunk``: the early check of an upload, before its audio is decoded.
2. ``MeetingClosed``: the same check inside ``Store.add_chunk``'s transaction, for an
   upload that passed the early check while the worker started processing.
3. A chunk (or chat) that arrives while the worker processes the session.
Fictional data only."""

from __future__ import annotations

import threading

import numpy as np

from athena.meetings.ingest.chat import ChatIn, ingest_chat
from tests.conftest import SAMPLE_RATE, silence, tone, wav

T0 = 1_760_000_000.0


def chunk(*parts):
    out = np.zeros(30 * SAMPLE_RATE, np.float32)
    for at, p in parts:
        out[int(at * SAMPLE_RATE):int(at * SAMPLE_RATE) + p.size] += p
    return wav(out)


def upload(pipeline, session, seq, start, data, track=None, name=None, device="zoom"):
    return pipeline.ingest_chunk(device=device, session_id=session, sequence=seq,
                                 started_at=T0 + start, codec="wav", data=data, track=track,
                                 track_name=name)


def a_meeting(pipeline, session="m1"):
    upload(pipeline, session, 0, 0, chunk((0, tone(300, 5))), "a", "Alice Example")
    pipeline.end_session(session)


def during_whisper(pipeline, action):
    """Run ``action`` once, inside the worker's first whisper call."""
    original, fired = pipeline.transcriber.transcribe, []

    def transcribe(audio, prompt=""):
        if not fired:
            fired.append(action())
        return original(audio, prompt)

    pipeline.transcriber.transcribe = transcribe
    return fired


# --------------------------------------------------- 1. the early check (_late_meeting_chunk)
def test_a_chunk_after_the_meeting_was_processed_is_refused_before_decoding(pipeline):
    a_meeting(pipeline)
    pipeline.process_session("m1")
    detect_calls = []
    detect = pipeline.vad.detect
    pipeline.vad.detect = lambda pcm: detect_calls.append(1) or detect(pcm)
    late = upload(pipeline, "m1", 1, 30, chunk((0, tone(300, 5))), "a")
    assert (late.stop, late.reason, late.duplicate) == (True, "client", False)
    assert detect_calls == []  # refused before decode and VAD
    assert len(pipeline.store.chunks("m1")) == 1
    assert pipeline.store.get_session("m1")["status"] == "done"


def test_a_chunk_after_the_end_but_before_the_worker_is_stored_and_processed(pipeline):
    a_meeting(pipeline)  # status 'ended': the last round is still taken
    late = upload(pipeline, "m1", 1, 30, chunk((0, tone(300, 5))), "a")
    assert (late.stop, late.reason, late.duplicate) == (True, "client", False)
    assert len(pipeline.store.chunks("m1")) == 2
    pipeline.process_pending()
    (conv,) = pipeline.store.session_conversations("m1")
    assert len(pipeline.store.conversation_segments(conv)) == 2


# ------------------------------------- 2. the check in the store's transaction (MeetingClosed)
def test_a_chunk_whose_early_check_passed_as_the_worker_started_is_refused(pipeline,
                                                                          settings):
    a_meeting(pipeline)
    detect = pipeline.vad.detect

    def worker_starts_now(pcm):  # between the early check and add_chunk
        pipeline.store.set_session_status("m1", "processing")
        return detect(pcm)

    pipeline.vad.detect = worker_starts_now
    late = upload(pipeline, "m1", 1, 30, chunk((0, tone(300, 5))), "a")
    assert (late.stop, late.reason, late.duplicate) == (True, "client", False)
    assert len(pipeline.store.chunks("m1")) == 1
    assert pipeline.store.get_session("m1")["status"] == "processing"  # left to the worker
    files = sorted(p.name for p in (settings.audio_dir / "zoom" / "m1").iterdir())
    assert len(files) == 1 and not files[0].endswith(".part")  # its temp file is gone


def test_a_retry_racing_the_worker_is_still_a_duplicate(pipeline):
    a_meeting(pipeline)
    detect = pipeline.vad.detect

    def worker_starts_now(pcm):
        pipeline.store.set_session_status("m1", "processing")
        return detect(pcm)

    pipeline.vad.detect = worker_starts_now
    retry = upload(pipeline, "m1", 0, 0, chunk((0, tone(300, 5))), "a")
    assert (retry.stop, retry.reason, retry.duplicate) == (True, "client", True)


# ----------------------------------------------- 3. arriving while the worker processes
def test_a_meeting_chunk_during_processing_is_refused_and_the_pass_finishes(pipeline):
    a_meeting(pipeline)
    got = during_whisper(pipeline, lambda: upload(pipeline, "m1", 1, 30,
                                                  chunk((0, tone(300, 5))), "a"))
    pipeline.process_pending()
    assert got[0].stop and got[0].reason == "client"
    assert len(pipeline.store.chunks("m1")) == 1
    assert pipeline.store.get_session("m1")["status"] == "done"
    assert pipeline.process_pending() == 0


def test_a_plain_chunk_during_processing_sends_the_session_back(pipeline):
    upload(pipeline, "s1", 0, 0, chunk((0, tone(200, 5))), device="omi")
    pipeline.end_session("s1")
    got = during_whisper(pipeline, lambda: upload(pipeline, "s1", 1, 30,
                                                  chunk((0, tone(600, 5))), device="omi"))
    pipeline.process_pending()
    assert got[0].stop and not got[0].duplicate
    assert pipeline.store.get_session("s1")["status"] == "ended"  # not marked done
    assert pipeline.process_pending() == 1  # the next pass takes the new chunk
    (conv,) = pipeline.store.session_conversations("s1")
    assert len(pipeline.store.conversation_segments(conv)) == 2


def test_a_first_tracked_chunk_during_processing_files_the_pass_as_a_meeting(pipeline):
    """A session stopped on silence is being processed as a plain recording when its
    first tracked chunk comes and reopens it: the pass, which read the chunks before,
    transcribes them as a plain recording, but files them as a meeting (never joined
    with the plain conversation 100 s before it)."""
    upload(pipeline, "p1", 0, -100, chunk((0, tone(600, 5))), device="bot-test")
    pipeline.end_session("p1")
    pipeline.process_session("p1")
    upload(pipeline, "s1", 0, 0, chunk((0, tone(300, 5))), device="bot-test")
    pipeline.end_session("s1", "silence")
    got = during_whisper(pipeline, lambda: upload(
        pipeline, "s1", 0, 90, chunk((0, tone(500, 5))), "t1", "Bo Example",
        device="bot-test"))
    pipeline.process_session("s1")
    assert got[0].stop is False  # reopened: recording again
    assert pipeline.store.get_session("s1")["status"] == "recording"
    (first,) = pipeline.store.session_conversations("p1")
    (meeting,) = pipeline.store.session_conversations("s1")
    assert meeting != first
    assert [s.speaker for s in pipeline.store.conversation_segments(meeting)] == [
        "Speaker 1"]  # diarized, not named by track: this pass read no tracked chunk
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    assert pipeline.store.session_conversations("s1") == [meeting]
    assert [s.speaker for s in pipeline.store.conversation_segments(meeting)] == [
        "Speaker 1", "Bo Example"]


def test_chat_during_processing_sends_the_meeting_back_for_one_more_pass(pipeline):
    a_meeting(pipeline)
    body = ChatIn.model_validate({"topic": "Planning", "lines": [
        {"at": T0 + 3, "from": "Alice Example", "zoom_user_id": "a", "text": "agenda?"}]})
    got = during_whisper(pipeline, lambda: ingest_chat(pipeline, "m1", body))
    pipeline.process_pending()
    assert got[0]["result"] == "queued"
    assert pipeline.store.get_session("m1")["status"] == "ended"
    assert pipeline.process_pending() == 1
    (conv,) = pipeline.store.session_conversations("m1")
    assert [s.kind for s in pipeline.store.conversation_segments(conv)] == ["speech", "chat"]


# --------------------------------------------------------------- with real threads
def test_uploads_racing_a_processing_thread(pipeline):
    """The worker runs in its own thread and is held inside whisper while the uploads
    come: the meeting's chunk is refused, the plain session's is taken (uploads never
    wait for the process lock)."""
    a_meeting(pipeline)
    upload(pipeline, "s1", 0, 0, chunk((0, tone(200, 5))), device="omi")
    pipeline.end_session("s1")
    inside, release = threading.Event(), threading.Event()
    original = pipeline.transcriber.transcribe

    def held(audio, prompt=""):
        inside.set()
        assert release.wait(10)
        return original(audio, prompt)

    pipeline.transcriber.transcribe = held
    worker = threading.Thread(target=pipeline.process_session, args=("m1",))
    worker.start()
    try:
        assert inside.wait(10)
        meeting = upload(pipeline, "m1", 1, 30, chunk((0, tone(300, 5))), "a")
        plain = upload(pipeline, "s1", 1, 30, wav(silence(30)), device="omi")
    finally:
        release.set()
        worker.join(10)
    assert (meeting.stop, meeting.reason) == (True, "client")
    assert len(pipeline.store.chunks("m1")) == 1
    assert pipeline.store.get_session("m1")["status"] == "done"
    assert not plain.duplicate and len(pipeline.store.chunks("s1")) == 2
