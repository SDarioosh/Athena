"""Regressions for how a recorded meeting is processed: chunks per track, finalize and
idle rules, one conversation per meeting, chat lines. Fictional data only."""

from __future__ import annotations

from athena.domain import CHAT
from tests.conftest import silence, wav
from tests.meetings.test_meeting_chat import chat
from tests.meetings.test_meeting_tracks import ALICE, BO, T0, ScriptedWhisper

SILENT_1S = wav(silence(1))


def chat_only_meeting(pipeline, client, session, lines, *, end_at):
    """What the bot does when no audio was ever uploaded (permission denied, everyone
    muted): one second of silence without a track at the end, then the chat call."""
    pipeline.transcriber = ScriptedWhisper()
    pipeline.ingest_chunk(device="zoom", session_id=session, sequence=0,
                          started_at=end_at - 1, codec="wav", data=SILENT_1S)
    r = chat(client, {"topic": f"Topic {session}", "lines": lines}, session=session)
    assert r.status_code == 200, r.text
    pipeline.end_session(session)
    pipeline.process_session(session)


def line(at, text, who=ALICE):
    return {"at": at, "from": who[1], "zoom_user_id": who[0], "text": text}


def test_chat_only_meeting_is_one_conversation_across_long_gaps(pipeline, client):
    """A chat-only meeting (the bot's untracked silence chunk) whose chat has a pause
    longer than CONVO_CONVERSATION_GAP_SECONDS stays one conversation, all of it the
    meeting's: a meeting is never split at gaps."""
    gap = pipeline.settings.conversation_gap_seconds
    lines = [line(T0 + 10, "Can everyone see my screen?"),
             line(T0 + 10 + gap + 600, "Here is the agenda.", BO),
             line(T0 + 10 + 2 * gap + 1200, "Thanks, bye.")]
    chat_only_meeting(pipeline, client, "zoom-chat", lines, end_at=T0 + 3 * gap + 1300)
    segs = pipeline.store.session_segments("zoom-chat")
    assert [s.kind for s in segs] == [CHAT] * 3
    assert len({s.conversation_id for s in segs}) == 1
    conv_id = segs[0].conversation_id
    assert pipeline.store.meeting_conversation("zoom-chat") == conv_id
    assert pipeline.store.get_conversation(conv_id)["meeting"]["topic"] == "Topic zoom-chat"


def test_back_to_back_chat_only_meetings_stay_two(pipeline, client):
    """Two chat-only meetings within the conversation gap are never joined."""
    chat_only_meeting(pipeline, client, "zoom-a", [line(T0 + 10, "First meeting line.")],
                      end_at=T0 + 60)
    chat_only_meeting(pipeline, client, "zoom-b", [line(T0 + 90, "Second meeting line.")],
                      end_at=T0 + 120)
    a = pipeline.store.meeting_conversation("zoom-a")
    b = pipeline.store.meeting_conversation("zoom-b")
    assert a and b and a != b
    assert pipeline.store.get_conversation(a)["meeting"]["topic"] == "Topic zoom-a"
    assert pipeline.store.get_conversation(b)["meeting"]["topic"] == "Topic zoom-b"


# ------------------------------------------------------------ idle finalize


def _bot_row(pipeline, session, created_at, state="recording"):
    pipeline.store.add_meeting({"id": f"bot-{session}", "state": state,
                                "session_id": session, "meeting_id": "85012345678",
                                "created_at": created_at})


def test_idle_does_not_end_a_meeting_whose_bot_still_runs(pipeline):
    """Zoom sends a participant's audio only while they talk, so a meeting where everyone
    is muted for longer than CONVO_SESSION_IDLE_SECONDS uploads nothing. While its bot is
    still running (an active meetings row) the idle rule must not end the session:
    processing it would answer the bot's next chunk ``stop: true`` and the bot would
    leave the meeting it is still recording. The bot's process ending ends the session
    (meetings.py); a bot gone silently (server restarted) is ended once its lifetime is
    surely over."""
    now = T0 + 1000
    _bot_row(pipeline, "zoom-m1", created_at=now - 40)
    pipeline.ingest_chunk(device="zoom", session_id="zoom-m1", sequence=0, started_at=T0,
                          codec="wav", data=wav(silence(30)), now=now, track=ALICE[0],
                          track_name=ALICE[1])
    idle = pipeline.settings.session_idle_seconds
    assert pipeline.finalize_idle(now + idle + 1) == []
    assert pipeline.store.get_session("zoom-m1")["status"] == "recording"
    # the bot can't outlive CONVO_MAX_SESSION_SECONDS: then the idle rule applies again
    late = now + pipeline.settings.max_session_seconds + 2 * idle + 600
    assert pipeline.finalize_idle(late) == ["zoom-m1"]


def test_idle_still_ends_a_meeting_whose_bot_has_exited(pipeline):
    now = T0 + 1000
    _bot_row(pipeline, "zoom-m2", created_at=now - 40, state="ended")
    pipeline.ingest_chunk(device="zoom", session_id="zoom-m2", sequence=0, started_at=T0,
                          codec="wav", data=wav(silence(30)), now=now, track=ALICE[0],
                          track_name=ALICE[1])
    idle = pipeline.settings.session_idle_seconds
    assert pipeline.finalize_idle(now + idle + 1) == ["zoom-m2"]


# ------------------------------------------------------------ voices


def test_two_participants_with_one_name_teach_no_voice(pipeline):
    """Two Zoom users showing the same name ("Alex Example" twice) are one speaker name
    but maybe two people: their tracks' samples would mix two voices under one person
    (in fact the second track's replaced the first's), and the relabel would put that
    name on strangers in other recordings. Neither track is learned."""
    import numpy as np

    from tests.conftest import SAMPLE_RATE, tone

    def track(start, secs, freq):
        out = np.zeros(30 * SAMPLE_RATE, np.float32)
        t = tone(freq, secs)
        out[int(start * SAMPLE_RATE):int(start * SAMPLE_RATE) + t.size] += t
        return wav(out)

    pipeline.transcriber = ScriptedWhisper()
    for tid, data in (("16778240", track(0, 13, 300)), ("16779264", track(15, 14, 500))):
        pipeline.ingest_chunk(device="zoom", session_id="m1", sequence=0, started_at=T0,
                              codec="wav", data=data, track=tid, track_name="Alex Example")
    pipeline.end_session("m1")
    pipeline.process_session("m1")
    assert [s.speaker for s in pipeline.store.session_segments("m1")] == ["Alex Example"] * 2
    assert pipeline.store.voice_samples("Alex Example") == []
