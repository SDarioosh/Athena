"""Who is speaking in a short clip (the hub's voice check of a spoken command).

    POST /v1/speakers/identify    multipart ``audio`` (any format ffmpeg decodes; a voice
                                  satellite sends WAV 16 kHz mono), optional ``codec``

Answer::

    {"speaker": {"id": "12", "name": "Sam", "is_owner": true} | null,
     "score": 0.71 | null, "threshold": 0.5, "speech_seconds": 2.4}

The clip is decoded, its speech found with the VAD, and the speech embedded with the
diarizer's speaker embedder (ECAPA); the embedding is scored against every enrolled voice
the way diarization scores a speaker (``EnrolledVoice.score``: the mean of the closest few
samples). ``speaker`` is the best-scoring voice when it reaches the threshold
(``CONVO_SPEAKER_IDENTIFY_THRESHOLD``, unset = ``CONVO_SPEAKER_MATCH_THRESHOLD``) and, with
``CONVO_SPEAKER_MATCH_MARGIN``, beats the runner-up by that margin; else null. ``score`` is
the best score (null when nothing was compared: less than ``MIN_SPEECH_SECONDS`` of speech
or no enrolled voice). ``id`` is the person's stable id (as ``list_people``), as a string,
or null when the voice has no person.

The audio is only held in memory for the call: never stored, logged or kept, and nothing
is written to the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from . import audio as audio_mod
from .diarize import load_voices
from .serializers import owner_names

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

MAX_IDENTIFY_BYTES = 2 * 1024 * 1024
MAX_IDENTIFY_SECONDS = 20.0
MIN_SPEECH_SECONDS = 1.0


class IdentifyError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def threshold(pipeline: Pipeline) -> float:
    s = pipeline.settings
    return (s.speaker_identify_threshold if s.speaker_identify_threshold is not None
            else s.speaker_match_threshold)


def identify(pipeline: Pipeline, data: bytes, codec: str = "wav") -> dict[str, Any]:
    """The answer above for the uploaded clip ``data`` (blocking: run it in a thread)."""
    diarizer = pipeline.diarizer
    if diarizer is None:
        raise IdentifyError(503, "diarization is not enabled (CONVO_DIARIZE=false), so "
                                 "there are no voices to compare")
    if not data:
        raise IdentifyError(400, "empty audio")
    if len(data) > MAX_IDENTIFY_BYTES:
        raise IdentifyError(413, f"audio is larger than {MAX_IDENTIFY_BYTES} bytes")
    try:
        pcm = audio_mod.decode(data, codec)
    except audio_mod.AudioDecodeError as e:
        raise IdentifyError(422, f"could not decode audio: {e}") from e
    if audio_mod.duration_seconds(pcm) > MAX_IDENTIFY_SECONDS + 0.5:
        raise IdentifyError(413, f"audio is longer than {MAX_IDENTIFY_SECONDS:.0f} s")
    sr = audio_mod.SAMPLE_RATE
    spans = pipeline.vad.detect(pcm)
    speech = (np.concatenate([pcm[int(s.start * sr):int(s.end * sr)] for s in spans])
              if spans else np.zeros(0, np.float32))
    seconds = audio_mod.duration_seconds(speech)
    limit = threshold(pipeline)
    out: dict[str, Any] = {"speaker": None, "score": None, "threshold": round(limit, 3),
                           "speech_seconds": round(seconds, 1)}
    voices = load_voices(pipeline.store)  # the latest enrollments, not the worker's copy
    if seconds < MIN_SPEECH_SECONDS or not voices:
        return out
    emb = diarizer.embedder.embed(speech)
    scores = sorted(((v.score(emb), i) for i, v in enumerate(voices)), reverse=True)
    best, i = scores[0]
    out["score"] = round(float(best), 3)
    margin = diarizer.match_margin
    clear = margin <= 0 or len(scores) < 2 or best - scores[1][0] >= margin
    if best >= limit and clear:
        voice = voices[i]
        store = pipeline.store
        pid = store.owner_person_id(owner_names(pipeline.settings)) if voice.is_owner \
            else None
        if pid is None:
            pid = store.person_id(voice.name)
        out["speaker"] = {"id": None if pid is None else str(pid), "name": voice.name,
                          "is_owner": voice.is_owner}
    return out
