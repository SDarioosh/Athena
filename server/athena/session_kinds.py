"""Session kinds: how the worker turns each kind of recording into conversations.

The worker picks the kind once per pass, when it starts processing a session
(:func:`kind_of`), and asks the kind what to do at each step:

* :data:`PLAIN`: a walk-around recording (an untracked session of any device but
  ``zoom``). Its speakers are diarized, it is split into conversations at speech gaps
  over ``CONVO_CONVERSATION_GAP_SECONDS``, and a part near another conversation of the
  same device joins it (never a meeting's).
* :data:`NOTE`: a voice note (session kind ``note``). Every line is the owner's (no
  diarizer), one conversation of kind ``note``, never split, never joined.
* :data:`MEETING`: a session with tracks, or any session of device ``zoom`` (the bot's
  chat-only meeting has one untracked second of silence; QA BUG-27), or one with meeting
  info. One conversation, never split at gaps, never joined with another session's.
  Tracked chunks are transcribed track by track and named by their track
  (``meetings/transcribe/tracks.py``).

The steps (``Pipeline.process_session`` calls them in this order):

1. :meth:`SessionKind.transcribe`: the session's chunks that still have audio, into lines.
2. :meth:`SessionKind.after_transcribe`: once the lines are stored, while the audio is
   still there: voices learned from named meeting tracks, live transcripts dropped.
3. :meth:`SessionKind.split`: the lines into conversations; returns those to summarize.

and, per conversation, :func:`for_conversation` then :meth:`SessionKind.summary_context`
(a conversation, not a session: one conversation can hold lines of several sessions;
a meeting's is marked by its meeting info).

What may change while a pass runs. Uploads never wait for the worker, so the store can
change between the steps; tracks and meeting info are only ever added, never removed,
and a session's device and kind never change. So a NOTE stays a note and a MEETING
stays a meeting, but a PLAIN session becomes a meeting when its first tracked chunk
comes, which the store accepts during processing only if an untracked chunk stopped the
session on silence (``Store.add_chunk``, QA BUG-16). A PLAIN pass therefore reads the
tracks again at each step, exactly where it did before kinds existed: transcription
after it listed the chunks, voices before learning, the meeting check before splitting.
The chunk then also sends the session back to ``recording``, so another pass follows.

Uploads (before any pass) only ask whether the session has tracks, whatever its kind:
:func:`stops_on_silence` and :func:`refuses_late_chunk`. A voice note with tracks never
stops on silence either, and a chat-only Zoom session (a MEETING when processed) does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from .conversations import split_on_gaps
from .domain import MEETING_CLOSED_STATUSES, NOTE, ZOOM_DEVICE, Segment

if TYPE_CHECKING:  # pragma: no cover
    from .db.store import Store
    from .pipeline import Pipeline

Kept = dict[str, list[tuple[float, float]]]
Transcribed = tuple[list[Segment], list[tuple[float, float]], list[dict[str, Any]]]


# ---------------------------------------------------------------------- uploads
def stops_on_silence(store: Store, session_id: str, track: str) -> bool:
    """Whether trailing silence may stop the session this chunk (``track``, '' = none)
    belongs to: not once it has a tracked chunk (a meeting: a quiet break doesn't end
    it; it stops at the max duration, on Stop, or idle)."""
    return not (track or store.has_tracks(session_id))


def refuses_late_chunk(store: Store, sess: dict[str, Any] | None, track: str,
                       sequence: int) -> bool:
    """Whether a new chunk of a meeting that is being or was already processed is
    refused (``Pipeline._late_meeting_chunk``; ``Store.add_chunk`` makes the same check
    in its transaction and raises ``MeetingClosed``). A retry of a stored chunk is not
    refused (it is answered ``duplicate``), nor is the first tracked chunk of a session
    an untracked chunk stopped on silence."""
    return not (sess is None or sess["status"] not in MEETING_CLOSED_STATUSES
                or not ((track and sess["stop_reason"] != "silence")
                        or store.has_tracks(sess["id"]))
                or store.has_chunk(sess["id"], track, sequence))


# ------------------------------------------------------------------------ kinds
class SessionKind:
    """What one kind of session does at each processing step (see the module doc)."""

    name: ClassVar[str]
    #: lines are the owner's; no diarizer, no track naming
    owner_lines: ClassVar[bool] = False

    def __repr__(self) -> str:
        return f"<SessionKind {self.name}>"

    # 1 ----------------------------------------------------------------- transcribe
    def transcribe(self, p: Pipeline, session_id: str, kept: Kept | None) -> Transcribed:
        """Transcribe the chunks that still have audio. Returns the lines, the covered
        time ranges and the chunks that were transcribed (only their audio is deleted).
        For a meeting, ``kept`` gets each named track's kept lines."""
        chunks = [c for c in p.store.chunks(session_id) if c["audio_path"]]
        ranges = [(c["started_at"], c["started_at"] + c["duration"]) for c in chunks]
        if not any(c["speech"] for c in chunks):
            return [], ranges, chunks
        if p.transcriber is None:
            raise RuntimeError("no transcriber configured (set CONVO_WHISPER_SERVER_URL)")
        if self._by_track(p.store, session_id):
            # One track per participant, each transcribed on its own and named by its
            # track. Imported only here, so sessions without tracks never load it.
            from .meetings.transcribe import tracks as meeting_tracks

            segments = meeting_tracks.transcribe_meeting(p, session_id, chunks, kept)
        else:
            segments = p._transcribe_recording(session_id, chunks,
                                               owner_lines=self.owner_lines)
        return segments, ranges, chunks

    def _by_track(self, store: Store, session_id: str) -> bool:
        """Transcribed track by track: it has tracks (read now, after the chunks)."""
        return store.has_tracks(session_id)

    # 2 ------------------------------------------------------------ after_transcribe
    def after_transcribe(self, p: Pipeline, session_id: str, segments: list[Segment],
                         done: list[dict[str, Any]], kept: Kept) -> None:
        """After the lines are stored, before the audio is deleted."""
        if p.settings.learn_meeting_voices and self._learns_voices(p.store, session_id,
                                                                   segments):
            # voice samples from named meeting tracks, while the audio is here
            from .meetings.people.voices import learn_from_meeting

            learn_from_meeting(p, session_id, done, kept)
        if any(c["track"] for c in done):
            # a meeting's live transcript (meetings/transcribe/live.py) is in its lines now
            p.store.drop_live_states(session_id)

    def _learns_voices(self, store: Store, session_id: str,
                       segments: list[Segment]) -> bool:
        return bool(segments) and store.has_tracks(session_id)

    # 3 ------------------------------------------------------------------- split
    def split(self, p: Pipeline, session_id: str, device: str) -> list[str]:
        """File the session's lines into conversations; returns those to summarize."""
        raise NotImplementedError

    # summaries -------------------------------------------------------------
    def summary_context(self, conv: dict[str, Any] | None, segments: list[Segment],
                        timezone: str = "UTC") -> dict[str, Any]:
        """Extra arguments for ``Summarizer.summarize`` of a conversation of this kind
        (``timezone``: CONVO_TIMEZONE, for times the model is told)."""
        return {}


class PlainKind(SessionKind):
    name = "plain"

    def split(self, p: Pipeline, session_id: str, device: str) -> list[str]:
        # it became a meeting since the pass began (see the module doc)
        if is_meeting(p.store, session_id, device):
            return MEETING.split(p, session_id, device)
        store, gap = p.store, p.settings.conversation_gap_seconds
        touched: list[str] = []
        for group in split_on_gaps(store.session_segments(session_id), gap):
            start, end = group[0].started_at, max(s.ended_at for s in group)
            near = [c["id"] for c in store.overlapping_conversations(device, start, end, gap)]
            meetings = store.meeting_conversations(near)  # never joined with a meeting
            near = [c for c in near if c not in meetings]
            conv_id = store.save_conversation(near[0] if near else None, device,
                                              [s.id for s in group if s.id is not None],
                                              absorb=near[1:])
            touched = [t for t in touched if t not in near] + [conv_id]
        return touched


class NoteKind(SessionKind):
    name = "note"
    owner_lines = True

    def _by_track(self, store: Store, session_id: str) -> bool:
        return False  # tracks or not, a voice note's lines are the owner's

    def _learns_voices(self, store: Store, session_id: str,
                       segments: list[Segment]) -> bool:
        return False

    def split(self, p: Pipeline, session_id: str, device: str) -> list[str]:
        """One conversation of kind NOTE: no gap split, never joined with nearby
        conversations. A late chunk extends the note's existing conversation."""
        segments = p.store.session_segments(session_id)
        if not segments:
            return []
        existing = p.store.session_conversations(session_id)
        conv_id = p.store.save_conversation(
            existing[0] if existing else None, device,
            [s.id for s in segments if s.id is not None], absorb=existing[1:], kind=NOTE)
        return [conv_id]


class MeetingKind(SessionKind):
    name = "meeting"

    def split(self, p: Pipeline, session_id: str, device: str) -> list[str]:
        """One conversation: never split at speech gaps (a break inside it), never joined
        with another session's conversation (two back-to-back meetings stay two). A pass
        after the first keeps its conversation."""
        segs = p.store.session_segments(session_id)
        if not segs:
            return []
        have = p.store.session_conversations(session_id)
        conv_id = p.store.save_conversation(have[0] if have else None, device,
                                            [s.id for s in segs if s.id is not None],
                                            absorb=have[1:])
        # its topic and participants (meetings/ingest/chat.py) show on this conversation
        p.store.link_meeting(session_id, conv_id)
        return [conv_id]

    def summary_context(self, conv: dict[str, Any] | None, segments: list[Segment],
                        timezone: str = "UTC") -> dict[str, Any]:
        assert conv is not None  # for_conversation: only a conversation with a meeting
        # an online meeting: its topic, people, chat lines
        from .meetings.ingest.chat import summary_context

        return {"context": summary_context(conv["meeting"], segments)}


class TextKind(SessionKind):
    """A chat's text messages (``texts/``): uploaded as lines, never transcribed; the
    upload threads them, so the worker never processes such a session."""
    name = "text"

    def split(self, p: Pipeline, session_id: str, device: str) -> list[str]:
        return []  # threads are made when messages are stored (TextsRepo._rethread)

    def summary_context(self, conv: dict[str, Any] | None, segments: list[Segment],
                        timezone: str = "UTC") -> dict[str, Any]:
        assert conv is not None and conv.get("thread")  # for_conversation: only a thread
        from .texts.summary import context

        return {"context": context(conv["thread"], segments, timezone)}


PLAIN, NOTE_KIND, MEETING, TEXT_KIND = PlainKind(), NoteKind(), MeetingKind(), TextKind()


def is_meeting(store: Store, session_id: str, device: str) -> bool:
    """Tracks, or a Zoom session without them (the bot's chat-only meeting: one
    untracked second of silence, then chat lines; QA BUG-27), or meeting info."""
    return (store.has_tracks(session_id) or device == ZOOM_DEVICE
            or store.has_meeting_info(session_id))


def kind_of(store: Store, sess: dict[str, Any]) -> SessionKind:
    """The kind of the session row ``sess``, read when a pass starts."""
    if sess.get("kind") == NOTE:
        return NOTE_KIND
    return MEETING if is_meeting(store, sess["id"], sess["device"]) else PLAIN


def for_conversation(conv: dict[str, Any] | None) -> SessionKind:
    """The kind whose summary rules a conversation (a ``Store.get_conversation`` row)
    gets: a thread's when it has a text block, a meeting's when it has a meeting block."""
    if conv and conv.get("thread"):
        return TEXT_KIND
    if conv and conv.get("meeting"):
        return MEETING
    return NOTE_KIND if conv and conv.get("kind") == NOTE else PLAIN
