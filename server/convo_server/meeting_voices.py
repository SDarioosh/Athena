"""Learning voices from meetings (Zoom bot, plan lane 2C).

In a meeting every named participant speaks on their own track, so Athena knows for sure
whose voice it hears. A few seconds of each track are saved as voice samples of that
person, so the diarizer recognizes them later in in-person recordings
(``CONVO_LEARN_MEETING_VOICES``, on by default; the owner's decision).

How samples are chosen, per track:

* Only a named participant's own track: not a shared room or device ("Conference Room",
  "iPhone": several voices), decided exactly as transcription does
  (``meeting_tracks._room_track``: any display name the track ever had), and not an
  unnamed "Speaker N" track. The owner's track counts
  only when the owner has an enrolled voice (``is_owner``); its samples go to that voice,
  so a meeting never makes a second owner or a non-owner speaker with the owner's name.
* The track needs at least ``MIN_SPEECH_SECONDS`` of VAD speech in total.
* Windows of ``WINDOW_SECONDS`` are cut from the track's own VAD speech, never while any
  other track has VAD speech (``OTHERS_PAD_SECONDS`` around it): no crosstalk and no bleed
  of another participant's voice. Up to ``MAX_CANDIDATES`` windows spread over the
  meeting are loaded; the ``SAMPLES_PER_MEETING`` loudest (clearest on their own mic)
  are embedded.

Storage: ``voice_samples`` rows with ``source='meeting'`` and the meeting's session id.
A person keeps at most ``MAX_MEETING_SAMPLES`` meeting samples (oldest replaced first);
enrolled and named samples are never touched. Processing a meeting again replaces that
meeting's own samples.

Then the existing relabel flow (``speakers.relabel_matches``, the thresholds
``name_speaker`` uses) renames unnamed speakers of other recordings whose stored voice
confidently matches a learned person.

Cost: only the speaker embedder that the worker already holds for diarization
(``Pipeline.diarizer.embedder``, ECAPA), on at most 3 windows of 3 s per person; never
the diarizer's clustering and never pyannote. Only the chunks holding chosen windows are
decoded again. Imported only from ``Pipeline.process_session`` for a meeting when the
setting is on, so nothing of this loads otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .audio import SAMPLE_RATE
from .diarize import rms_db

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

log = logging.getLogger(__name__)

WINDOW_SECONDS = 3.0
SAMPLES_PER_MEETING = 3
MAX_MEETING_SAMPLES = 3  # per person, besides enrolled and named samples
MIN_SPEECH_SECONDS = 10.0
# Another track's speech, widened by this much, is off limits (whisper/VAD edges).
OTHERS_PAD_SECONDS = 0.5
# Own VAD regions this close together count as one stretch of speech (a short pause).
MERGE_GAP_SECONDS = 0.5
# At most this many candidate windows per track are decoded to pick the loudest.
MAX_CANDIDATES = 12
# A window must hold at least this share of WINDOW_SECONDS of audio.
MIN_FILL = 0.95
CONTIGUOUS_TOLERANCE = 1.5  # as pipeline.CONTIGUOUS_TOLERANCE

Interval = tuple[float, float]


@dataclass(frozen=True)
class Learned:
    name: str
    samples: int
    relabeled: int  # unnamed speakers of other recordings renamed to this person


def merge(intervals: Sequence[Interval], gap: float = 0.0) -> list[Interval]:
    """Sorted, with intervals at most ``gap`` apart joined."""
    out: list[Interval] = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(intervals: Sequence[Interval], busy: Sequence[Interval]) -> list[Interval]:
    """``intervals`` minus ``busy`` (both sorted, non-overlapping)."""
    out: list[Interval] = []
    for a, b in intervals:
        cur = a
        for x, y in busy:
            if y <= cur or x >= b:
                continue
            if x > cur:
                out.append((cur, x))
            cur = max(cur, y)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return out


def intersect(intervals: Sequence[Interval], spans: Sequence[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in intervals:
        for x, y in spans:
            lo, hi = max(a, x), min(b, y)
            if hi > lo:
                out.append((lo, hi))
    return sorted(out)


def speech(chunks: Sequence[dict[str, Any]]) -> list[Interval]:
    """VAD speech of chunks, in epoch seconds."""
    return merge([(c["started_at"] + s, c["started_at"] + e)
                  for c in chunks for s, e in c["speech"]])


def runs(chunks: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Contiguous chunks of one track (same rule as ``Pipeline._runs``)."""
    out: list[list[dict[str, Any]]] = []
    for c in sorted(chunks, key=lambda c: c["sequence"]):
        if out:
            prev = out[-1][-1]
            gap = c["started_at"] - (prev["started_at"] + prev["duration"])
            if abs(gap) <= CONTIGUOUS_TOLERANCE and c["sequence"] == prev["sequence"] + 1:
                out[-1].append(c)
                continue
        out.append([c])
    return out


def candidate_windows(own: Sequence[dict[str, Any]], others: Sequence[dict[str, Any]],
                      ) -> list[tuple[float, float, list[dict[str, Any]]]]:
    """3 s windows of the track's own speech while no other track speaks:
    (start, end, the run's chunks), in time order, at most MAX_CANDIDATES spread evenly."""
    busy = merge([(a - OTHERS_PAD_SECONDS, b + OTHERS_PAD_SECONDS)
                  for a, b in speech(others)])
    free = subtract(merge(speech(own), MERGE_GAP_SECONDS), busy)
    out: list[tuple[float, float, list[dict[str, Any]]]] = []
    for run in runs(own):
        span = (run[0]["started_at"], run[-1]["started_at"] + run[-1]["duration"])
        for a, b in intersect(free, [span]):
            t = a
            while t + WINDOW_SECONDS <= b + 1e-6:
                out.append((t, t + WINDOW_SECONDS, run))
                t += WINDOW_SECONDS
    if len(out) > MAX_CANDIDATES:
        step = len(out) / MAX_CANDIDATES
        out = [out[int(i * step)] for i in range(MAX_CANDIDATES)]
    return out


class _Audio:
    """Decodes a track's chunks on demand, each at most once."""

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.cache: dict[tuple[str, int], np.ndarray] = {}

    def clip(self, start: float, end: float, run: Sequence[dict[str, Any]]) -> np.ndarray:
        parts = []
        for c in run:
            c0, c1 = c["started_at"], c["started_at"] + c["duration"]
            if c1 <= start or c0 >= end:
                continue
            key = (c.get("track") or "", c["sequence"])
            if key not in self.cache:
                self.cache[key] = self.pipeline._load_chunk(c)
            pcm = self.cache[key]
            parts.append(pcm[int(max(0.0, start - c0) * SAMPLE_RATE):
                             int(max(0.0, end - c0) * SAMPLE_RATE)])
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def choose_clips(pipeline: Pipeline, own: Sequence[dict[str, Any]],
                 others: Sequence[dict[str, Any]]) -> list[np.ndarray]:
    """Up to SAMPLES_PER_MEETING clips of WINDOW_SECONDS for one track ([] when it has
    under MIN_SPEECH_SECONDS of speech or no window free of other tracks' speech)."""
    total = sum(b - a for a, b in speech(own))
    if total < MIN_SPEECH_SECONDS:
        return []
    audio = _Audio(pipeline)
    clips = []
    for a, b, run in candidate_windows(own, others):
        clip = audio.clip(a, b, run)
        if clip.size >= MIN_FILL * WINDOW_SECONDS * SAMPLE_RATE:
            clips.append((rms_db(clip), a, clip))
    clips.sort(key=lambda x: (-x[0], x[1]))  # loudest first, then earliest
    return [c for _, _, c in clips[:SAMPLES_PER_MEETING]]


def learn_from_meeting(pipeline: Pipeline, session_id: str,
                       chunks: Sequence[dict[str, Any]]) -> list[Learned]:
    """Save voice samples of the meeting's named participants (see the module doc) and
    relabel matching unnamed speakers in other recordings. Call after the meeting's
    lines are stored and before its audio is deleted."""
    from .meeting_names import PLACEHOLDER
    from .meeting_tracks import _UNNAMED, _room_track, track_speaker
    from .speakers import relabel_matches

    store, cfg = pipeline.store, pipeline.settings
    by_track: dict[str, list[dict[str, Any]]] = {}
    for c in chunks:
        if c.get("audio_path") and c.get("track"):
            by_track.setdefault(c["track"], []).append(c)
    owner_voice = store.owner_voice_name()
    plan: list[tuple[str, str]] = []  # (track, speaker name)
    for info in store.tracks(session_id):
        track = info["track"]
        if track not in by_track:
            continue
        owner_names = cfg.zoom_owner_name_list
        name, owner = track_speaker(store, info, owner_names)
        if not owner and _room_track(info, owner_names):
            # transcription's own decision (any name the track ever had is a room or a
            # device): it was diarized, so several voices may be on it (QA BUG-01)
            continue
        if owner:
            if not owner_voice:
                log.info("meeting %s: owner's track not learned (no enrolled owner voice; "
                         "enroll with POST /v1/speakers/enroll is_owner=true)", session_id)
                continue
            name = owner_voice
        elif not name or name == PLACEHOLDER or _UNNAMED.match(name):
            continue  # an unnamed "Speaker N" or "Unknown": nobody to learn for
        plan.append((track, name))
    if not plan:
        return []
    if pipeline.diarizer is None or cfg.speaker_embedder != "ecapa":
        log.info("meeting %s: voices not learned (%s)", session_id,
                 "diarization is off" if pipeline.diarizer is None
                 else f"CONVO_SPEAKER_EMBEDDER={cfg.speaker_embedder}, not ecapa")
        return []
    chosen = []
    for track, name in plan:
        others = [c for t, cs in by_track.items() if t != track for c in cs]
        clips = choose_clips(pipeline, by_track[track], others)
        if clips:
            chosen.append((name, clips))
    if not chosen:
        return []
    # the speaker model is touched only now, for the chosen windows alone
    embedder = pipeline.diarizer.embedder
    learned: list[Learned] = []
    for name, clips in chosen:
        samples = [embedder.embed(c) for c in clips]
        store.put_meeting_voice_samples(name, session_id, samples, MAX_MEETING_SAMPLES)
        done = relabel_matches(store, name, cfg.speaker_relabel_threshold)
        learned.append(Learned(name, len(samples), len(done)))
        log.info("meeting %s: learned %d voice sample(s) of %s; relabeled %d unnamed "
                 "speaker(s) in other recordings (%d lines)", session_id, len(samples),
                 name, len(done), sum(r.lines for r in done))
    return learned
