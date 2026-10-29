"""Enroll a voice from the next recording (the hub's "Enroll my voice" window).

The owner starts it in the hub, reads the statements it shows while recording on the
watch, and stops. Recording on the watch matters: the voiceprint then sounds like the
owner's daily recordings (same microphone, same distance), which a phone or laptop
recording doesn't.

    GET    /v1/speakers/enroll/next   the state below
    POST   /v1/speakers/enroll/next   JSON {name?, is_owner? (default true)}: start waiting
    DELETE /v1/speakers/enroll/next   cancel while waiting

States: ``waiting`` (no recording yet, or one still coming in: ``recording`` describes it),
``processing``, ``done``, ``failed``, ``cancelled``, ``expired`` (nothing came in within
``WAIT_SECONDS``). Only one runs at a time; starting again replaces a waiting one.

Which recording: the first session whose first chunk reaches the server after the start
(server clock) and that started at most ``CLOCK_SLACK_SECONDS`` before it (device clock),
on any device but the Zoom bot and written notes, and not a meeting. A recording that was
already running when the owner pressed Start therefore never counts.

When the worker processes that session (``Pipeline.process_session`` asks :func:`claims`
first), its VAD speech is enrolled with :func:`diarize.enroll_voice` (the same as
``POST /v1/speakers/enroll``: it replaces earlier enrollment samples, keeps named and
learned ones) and unnamed speakers of earlier recordings that now match are renamed. The
recording itself is not transcribed and makes no conversation: read-aloud statements are
no conversation and would give the summarizer made-up facts. Its audio is deleted as usual
(``CONVO_KEEP_AUDIO`` keeps it).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from . import transcribe_engine
from .audio import SAMPLE_RATE
from .domain import WRITTEN_DEVICE, ZOOM_DEVICE

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

log = logging.getLogger(__name__)

WAIT_SECONDS = 30 * 60
CLOCK_SLACK_SECONDS = 120.0
# Speech needed for a good voiceprint: the API takes less, but the statements take ~75 s.
MIN_SPEECH_SECONDS = 30.0
SKIP_DEVICES = (ZOOM_DEVICE, WRITTEN_DEVICE)
ENDED = ("done", "failed", "cancelled", "expired")


class EnrollError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def default_name(pipeline: Pipeline) -> str:
    """Who the recording is enrolled as when the hub sends no name: the enrolled owner
    voice, else the owner's person, else ``CONVO_OWNER_NAME``."""
    store = pipeline.store
    return store.owner_name() or store.owner_person_name() or pipeline.settings.owner_name


def _candidate(pipeline: Pipeline, state: dict[str, Any]) -> dict[str, Any] | None:
    found = pipeline.store.recordings_since(state["started_at"],
                                            state["started_at"] - CLOCK_SLACK_SECONDS,
                                            SKIP_DEVICES)
    return found[0] if found else None


def _expire(pipeline: Pipeline, state: dict[str, Any] | None,
            now: float) -> dict[str, Any] | None:
    if (state and state["state"] == "waiting" and now > state["expires_at"]
            and _candidate(pipeline, state) is None):
        state = {**state, "state": "expired", "ended_at": now}
        pipeline.store.put_voice_enrollment(state)
    return state


def status(pipeline: Pipeline, now: float | None = None) -> dict[str, Any]:
    """The current or last enrollment, for the hub. ``state`` is ``none`` before any."""
    now = time.time() if now is None else now
    state = _expire(pipeline, pipeline.store.get_voice_enrollment(), now)
    out: dict[str, Any] = {
        "state": "none", "name": None, "is_owner": None, "started_at": None,
        "expires_at": None, "ended_at": None, "recording": None, "samples": None,
        "speech_seconds": None, "relabeled_lines": None, "error": None,
        **(state or {}),
        "default_name": default_name(pipeline),
        "owner_voice": pipeline.store.owner_name(),
        "min_speech_seconds": MIN_SPEECH_SECONDS,
        "available": pipeline.diarizer is not None,
    }
    if state and state["state"] == "waiting":
        rec = _candidate(pipeline, state)
        out["recording"] = None if rec is None else {
            "session_id": rec["id"], "device": rec["device"], "started_at": rec["started_at"],
            "seconds": round(rec["seconds"] or 0.0, 1), "status": rec["status"]}
    return out


def start(pipeline: Pipeline, name: str | None, is_owner: bool = True,
          now: float | None = None) -> dict[str, Any]:
    """Wait for the next recording. Replaces a waiting enrollment; refuses (409) while
    one is being processed."""
    now = time.time() if now is None else now
    if pipeline.diarizer is None:
        raise EnrollError(503, "diarization is not enabled (CONVO_DIARIZE=false), so there "
                               "are no voices to enroll")
    name = " ".join((name or "").split()) or default_name(pipeline)
    if not name:
        raise EnrollError(400, "name must not be empty")
    if len(name) > 100:
        raise EnrollError(400, "name is longer than 100 characters")
    current = pipeline.store.get_voice_enrollment()
    if current and current["state"] == "processing":
        raise EnrollError(409, "a recording is being enrolled right now; wait a moment")
    pipeline.store.put_voice_enrollment({
        "state": "waiting", "name": name, "is_owner": bool(is_owner), "started_at": now,
        "expires_at": now + WAIT_SECONDS, "ended_at": None, "session_id": None})
    log.info("voice enrollment: waiting for the next recording (as %s)", name)
    return status(pipeline, now)


def cancel(pipeline: Pipeline, now: float | None = None) -> dict[str, Any]:
    """Stop waiting. A recording already being enrolled can't be cancelled (409);
    an ended enrollment stays as it is."""
    now = time.time() if now is None else now
    state = _expire(pipeline, pipeline.store.get_voice_enrollment(), now)
    if state and state["state"] == "processing":
        raise EnrollError(409, "the recording is being enrolled right now")
    if state and state["state"] == "waiting":
        pipeline.store.put_voice_enrollment({**state, "state": "cancelled", "ended_at": now})
    return status(pipeline, now)


def claims(pipeline: Pipeline, session: dict[str, Any], now: float | None = None) -> bool:
    """Whether processing ``session`` is this enrollment's job: it is the waiting
    enrollment's recording, or the one an enrollment already used (a late chunk)."""
    state = pipeline.store.get_voice_enrollment()
    if not state:
        return False
    if state.get("session_id") == session["id"]:
        return True
    if state["state"] != "waiting":
        return False
    rec = _candidate(pipeline, state)
    return rec is not None and rec["id"] == session["id"]


def enroll_session(pipeline: Pipeline, session: dict[str, Any],
                   now: float | None = None) -> None:
    """Enroll the claimed ``session`` (call with the pipeline's process lock held,
    after :func:`claims`). The session ends ``done`` with no lines, or ``failed`` with
    its audio kept when something unexpected breaks."""
    from .diarize import enroll_voice, load_voices
    from .speakers import relabel_matches

    store, sid = pipeline.store, session["id"]
    state = store.get_voice_enrollment() or {}
    chunks = [c for c in store.chunks(sid) if c["audio_path"]]
    if state.get("session_id") == sid and state["state"] != "waiting":
        # a chunk that came in after the enrollment: nothing more to learn from it
        _finish(pipeline, session, chunks)
        return
    state = {**state, "state": "processing", "session_id": sid,
             "device": session["device"]}
    store.put_voice_enrollment(state)
    try:
        speech = []
        for c in chunks:
            pcm = transcribe_engine.load_chunk(c)
            speech += [pcm[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)] for a, b in c["speech"]]
        audio = np.concatenate(speech) if speech else np.zeros(0, np.float32)
        seconds = audio.size / SAMPLE_RATE
        if seconds < MIN_SPEECH_SECONDS:
            raise EnrollError(400, f"only {seconds:.0f} s of speech in the recording; "
                                   f"at least {MIN_SPEECH_SECONDS:.0f} s are needed. Start "
                                   "again and read all the statements")
        samples = enroll_voice(store, pipeline.diarizer.embedder, state["name"], audio,
                               state["is_owner"])
        pipeline.diarizer.voices = load_voices(store)
        relabeled = relabel_matches(store, state["name"],
                                    pipeline.settings.speaker_relabel_threshold)
    except Exception as e:
        expected = isinstance(e, (EnrollError, ValueError))
        if not expected:
            log.exception("voice enrollment from %s failed", sid)
        store.put_voice_enrollment({
            **state, "state": "failed", "ended_at": time.time() if now is None else now,
            "error": e.detail if isinstance(e, EnrollError) else
            str(e) if expected else f"{type(e).__name__}: {e}"})
        if expected:
            _finish(pipeline, session, chunks)
        else:  # keep the audio, like a failed transcription, so it can be looked at
            store.set_session_status(sid, "failed", f"voice enrollment: {e}",
                                     only_if="processing")
        return
    store.put_voice_enrollment({
        **state, "state": "done", "ended_at": time.time() if now is None else now,
        "samples": samples, "speech_seconds": round(seconds, 1),
        "relabeled_lines": sum(r.lines for r in relabeled), "error": None})
    log.info("voice enrollment: %s enrolled from %s (%d samples, %.0f s of speech; "
             "%d earlier lines renamed)", state["name"], sid, samples, seconds,
             sum(r.lines for r in relabeled))
    _finish(pipeline, session, chunks)


def _finish(pipeline: Pipeline, session: dict[str, Any],
            chunks: list[dict[str, Any]]) -> None:
    """The session is done without lines; its audio goes unless CONVO_KEEP_AUDIO."""
    if not pipeline.settings.keep_audio:
        pipeline._delete_audio(session["id"], session["device"], chunks)
    pipeline.store.set_session_status(session["id"], "done", only_if="processing")
