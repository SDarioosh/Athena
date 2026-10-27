"""Per-track transcription of a meeting (Zoom bot, plan lane 2A).

A meeting session holds one audio track per participant (``track`` / ``track_name`` on
each chunk). Each track is transcribed on its own, and every line from it is that
participant's: no diarizer, no speaker embedder. Then:

* **Crosstalk**: lines of different tracks may overlap in time; all are kept, ordered by
  start time. Lines are joined (``CONVO_JOIN_LINES``) within one track only, and
  whisper's context prompt never crosses tracks.
* **Bleed**: two participants in one room hear each other, so one voice can reach two
  microphones. A line whose words nearly match a line on another track at about the
  same time is dropped, keeping the louder track's line (``find_bleed``).
* **Shared rooms and devices**: a track whose display name is a room or a device
  ("Conference Room", "iPhone", "Zoom user", ``is_shared_room``) can hold several
  people. The configured diarizer runs on that track only; its unnamed voices become
  "Speaker N" (numbered across the meeting) with stored voices, so ``name_speaker`` and
  ``list_unnamed_speakers`` work on them as on any recording.
* **Owner**: a track named in ``CONVO_ZOOM_OWNER_NAMES`` is the owner's
  (``is_owner=true``), named as the owner is everywhere else.

Imported only by ``Pipeline._transcribe_session`` for a session that has tracks, so a
server that never sees a meeting never loads this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import numpy as np

from .audio import SAMPLE_RATE
from .diarize import UNSURE_LABEL, PieceRef, SpeakerLabel, load_voices, rms_db
from .lines import join_lines
from .store import Segment

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline
    from .store import Store

log = logging.getLogger(__name__)

# Bleed: a line is a copy of a line on another track when ...
# ... its words match that line's this well (``text_similarity``: the better of
# SequenceMatcher's ratio over words and, when the shorter line has at least
# BLEED_CONTAINED_MIN_WORDS words, the share of its words found in the other line in
# order). 0.85 keeps "I'll send the notes after lunch" / "I'll send the slides after
# lunch" (5 of 6 words, 0.83) apart while whisper's small slips on the quieter copy
# still match.
BLEED_MIN_SIMILARITY = 0.85
BLEED_CONTAINED_MIN_WORDS = 4
# ... and the two lines overlap in time by at least this share of the shorter line
# (whisper's line edges differ a little between the two copies). A line shorter than
# BLEED_MIN_SECONDS counts as that long.
BLEED_MIN_OVERLAP = 0.5
BLEED_MIN_SECONDS = 0.5

_UNNAMED = re.compile(r"^Speaker \d+$")
_WORD = re.compile(r"[\w']+")

# Display names that are a room or a device rather than one person (lowercase; matched
# as whole words). Zoom shows a Zoom Rooms system or a conference phone by its room or
# product name, a phone by its model, and a guest who never set a name as "Zoom user".
_ROOM_WORDS = (
    r"room", r"rooms", r"boardroom", r"conference", r"conf", r"meeting\s*room",
    r"huddle", r"auditorium", r"lobby", r"hall", r"zoom\s*user", r"zoomroom",
    r"polycom", r"poly\s+studio", r"logitech", r"cisco", r"webex", r"crestron",
    r"yealink", r"jabra", r"neat\s*(?:bar|board|pad)", r"meeting\s*owl", r"owl\s*pro",
    r"iphone", r"ipad", r"android", r"galaxy", r"pixel", r"samsung", r"oneplus",
    r"huawei", r"xiaomi", r"redmi", r"motorola", r"moto\s*g", r"sm-[a-z]?\d+\w*",
    r"macbook(?:\s*(?:pro|air))?", r"imac", r"laptop", r"desktop", r"chromebook",
    r"surface\s*(?:pro|hub|laptop)", r"phone", r"speakerphone", r"dial-?in", r"call-?in",
)
_ROOM = re.compile(r"(?<![\w-])(?:" + "|".join(_ROOM_WORDS) + r")(?![\w-])", re.IGNORECASE)
# A dial-in caller shows as their phone number.
_PHONE_NUMBER = re.compile(r"^[\s+()\d.-]{6,}$")


def is_shared_room(name: str | None) -> bool:
    """True when a Zoom display name looks like a room or a device, which can hold
    several people ("Conference Room", "Boardroom", "Meeting Room 2", "Anna's iPhone",
    "Galaxy S23", "Zoom user", "Polycom", a dial-in number), not one person's name."""
    n = " ".join((name or "").split())
    if not n:
        return False
    return bool(_ROOM.search(n) or _PHONE_NUMBER.match(n))


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def track_speaker(store: Store, track_info: dict[str, Any],
                  owner_names: Sequence[str]) -> tuple[str, bool]:
    """Who a track's lines belong to: (speaker name for its segments, is_owner).

    The one place a track becomes a speaker. This version: the track's latest display
    name, stripped, and the owner when it matches one of ``owner_names`` (any case).
    An empty name means the track has none; the caller numbers it "Speaker N".

    Seam for lane 2B: ``meeting_names.speaker_for_track`` has the same signature and
    adds name cleaning and matching to people; this function is wired to call it once
    both lanes are merged.
    """
    name = " ".join((track_info.get("name") or "").split())
    owner = bool(name) and any(_same_name(name, o) for o in owner_names if o.strip())
    return name, owner


@dataclass
class Line:
    start: float  # epoch seconds
    end: float
    text: str
    ref: PieceRef  # run index into the meeting's runs, seconds within that run
    track: str
    label: SpeakerLabel


def words_of(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def text_similarity(a: str, b: str) -> float:
    """How nearly two lines say the same words, 0..1 (see BLEED_MIN_SIMILARITY)."""
    wa, wb = words_of(a), words_of(b)
    if not wa or not wb:
        return 0.0
    matched = sum(m.size for m in SequenceMatcher(None, wa, wb, autojunk=False)
                  .get_matching_blocks())
    ratio = 2 * matched / (len(wa) + len(wb))
    short = min(len(wa), len(wb))
    contained = matched / short if short >= BLEED_CONTAINED_MIN_WORDS else 0.0
    return max(ratio, contained)


def same_time(a: Line, b: Line) -> bool:
    overlap = min(a.end, b.end) - max(a.start, b.start)
    shorter = max(BLEED_MIN_SECONDS, min(a.end - a.start, b.end - b.start))
    return overlap >= BLEED_MIN_OVERLAP * shorter


def find_bleed(lines: Sequence[Line], loudness: Sequence[float]) -> set[int]:
    """Indexes of lines that are another track's voice picked up by this track's mic.

    Two lines on different tracks that overlap in time (``same_time``) and say nearly
    the same words (``text_similarity`` >= BLEED_MIN_SIMILARITY): the quieter one is
    dropped (``loudness``: dBFS of each line's audio on its own track; a tie keeps the
    line with more words). Best matches are decided first, and a dropped line never
    causes another drop.
    """
    order = sorted(range(len(lines)), key=lambda i: lines[i].start)
    pairs: list[tuple[float, int, int]] = []
    for x, i in enumerate(order):
        a = lines[i]
        for j in order[x + 1:]:
            b = lines[j]
            if b.start > a.end + BLEED_MIN_SECONDS:
                break  # sorted by start: no later line can overlap a
            if a.track == b.track or not same_time(a, b):
                continue
            sim = text_similarity(a.text, b.text)
            if sim >= BLEED_MIN_SIMILARITY:
                pairs.append((sim, i, j))
    dropped: set[int] = set()
    for sim, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if i in dropped or j in dropped:
            continue
        key_i = (loudness[i], len(words_of(lines[i].text)))
        key_j = (loudness[j], len(words_of(lines[j].text)))
        quiet, loud = (j, i) if key_i >= key_j else (i, j)
        dropped.add(quiet)
        log.info("bleed: dropped a line on track %s (%.1f dBFS), same words as track %s "
                 "(%.1f dBFS, similarity %.2f)", lines[quiet].track, loudness[quiet],
                 lines[loud].track, loudness[loud], sim)
    return dropped


def meeting_prompt(base: str, names: Sequence[str], max_names: int) -> str:
    """Whisper's prompt for a meeting: the usual prompt plus the participants' display
    names it doesn't list yet, so whisper spells them right."""
    have = base.casefold()
    extra: list[str] = []
    for n in names:
        if n and n.casefold() not in have and n.casefold() not in {e.casefold() for e in extra}:
            extra.append(n)
    extra = extra[:max_names]
    return " ".join(p for p in (base, ", ".join(extra) + "." if extra else "") if p)


def transcribe_meeting(pipeline: Pipeline, session_id: str,
                       chunks: list[dict[str, Any]]) -> list[Segment]:
    """Lines of a tracked session: each track transcribed on its own and named by its
    track (shared-room tracks by the diarizer), bleed dropped, merged in time order."""
    from .pipeline import MAX_PROMPT_NAMES

    store, cfg = pipeline.store, pipeline.settings
    owner_names = cfg.zoom_owner_name_list
    owner_display = store.owner_name() or cfg.owner_name
    infos = {t["track"]: t for t in store.tracks(session_id)}
    by_track: dict[str, list[dict[str, Any]]] = {}
    for c in chunks:
        by_track.setdefault(c.get("track") or "", []).append(c)
    # tracks in order of first audio; chunks without a track (none from the bot) last
    order = sorted(by_track, key=lambda t: (t == "", infos.get(t, {}).get("started_at", 0.0),
                                            t))
    who = {t: track_speaker(store, infos.get(t, {"track": t, "name": None}), owner_names)
           for t in order if t}
    prompt = pipeline.whisper_prompt()
    if cfg.whisper_prompt_names:
        prompt = meeting_prompt(prompt, [n for t, (n, _) in who.items()
                                         if n and not is_shared_room(n)], MAX_PROMPT_NAMES)

    runs: list[np.ndarray] = []
    lines: list[Line] = []
    candidates: dict[str, np.ndarray] = {}
    unnamed = 0  # "Speaker N" numbering across the meeting
    voices_loaded = False
    for track in order:
        first_run = len(runs)
        pieces: list[tuple[float, float, str, PieceRef]] = []
        words: list[list[tuple[float, float, str]]] = []
        context = ""  # whisper's context starts again on every track
        for run_start, audio, speech in pipeline._runs(by_track[track]):
            runs.append(audio)
            got, got_words, context = pipeline._whisper_run(len(runs) - 1, run_start, audio,
                                                            speech, prompt, context)
            pieces += got
            words += got_words
        if not pieces:
            continue
        idx = sorted(range(len(pieces)), key=lambda i: pieces[i][0])
        pieces, words = [pieces[i] for i in idx], [words[i] for i in idx]
        name, owner = who.get(track, ("", False))
        shared = not track or (not owner and is_shared_room(name))
        if shared and pipeline.diarizer is not None:
            if not voices_loaded:
                pipeline.diarizer.voices = load_voices(store)
                voices_loaded = True
            labels, found, pieces = _diarize_track(pipeline, runs[first_run:], first_run,
                                                   pieces, words)
            renumber: dict[str, str] = {}
            for lab in labels:
                if _UNNAMED.match(lab.name) and lab.name not in renumber:
                    unnamed += 1
                    renumber[lab.name] = f"Speaker {unnamed}"
            labels = [SpeakerLabel(renumber.get(lab.name, lab.name), lab.is_owner)
                      for lab in labels]
            candidates.update({renumber[k]: v for k, v in found.items() if k in renumber})
        else:
            if owner:
                name = owner_display or name
            elif not name:
                unnamed += 1
                name = f"Speaker {unnamed}"
            labels = [SpeakerLabel(name, owner)] * len(pieces)
        if cfg.join_lines:  # within this track only: never joins two participants
            pieces, joined = join_lines(pieces, labels, cfg.join_gap_seconds,
                                        cfg.join_max_seconds)
            labels = joined if joined is not None else labels
        lines += [Line(s, e, text, ref, track, lab)
                  for (s, e, text, ref), lab in zip(pieces, labels, strict=True)]
    if pipeline.diarizer is not None and voices_loaded:
        store.put_speaker_candidates(session_id, candidates)

    loud = [rms_db(runs[ln.ref.run][int(ln.ref.start * SAMPLE_RATE):
                                    int(ln.ref.end * SAMPLE_RATE)]) for ln in lines]
    drop = find_bleed(lines, loud)
    rank = {t: i for i, t in enumerate(order)}
    kept = sorted((ln for i, ln in enumerate(lines) if i not in drop),
                  key=lambda ln: (ln.start, rank[ln.track], ln.end))
    return [Segment(started_at=ln.start, ended_at=ln.end, text=ln.text,
                    speaker=ln.label.name, is_owner=ln.label.is_owner) for ln in kept]


def _diarize_track(pipeline: Pipeline, runs: list[np.ndarray], first_run: int,
                   pieces: list[tuple[float, float, str, PieceRef]],
                   words: list[list[tuple[float, float, str]]]
                   ) -> tuple[list[SpeakerLabel], dict[str, np.ndarray],
                              list[tuple[float, float, str, PieceRef]]]:
    """The configured diarizer on one shared-room track: (labels, unnamed voices,
    pieces). Same steps as a recording (line split and line check when configured),
    on this track's runs only."""
    diarizer, cfg = pipeline.diarizer, pipeline.settings
    local = [(s, e, t, PieceRef(r.run - first_run, r.start, r.end)) for s, e, t, r in pieces]
    labels, found = diarizer.diarize_runs(runs, [p[3] for p in local])
    if cfg.speaker_split_lines:
        local, labels = pipeline._split_pieces(runs, local, words, labels)
    if cfg.speaker_line_check:
        labels = diarizer.check_lines(runs, [p[3] for p in local], labels,
                                      cfg.speaker_line_floor)
    back = [(s, e, t, PieceRef(r.run + first_run, r.start, r.end)) for s, e, t, r in local]
    return list(labels), found, back


__all__ = ["BLEED_MIN_OVERLAP", "BLEED_MIN_SIMILARITY", "UNSURE_LABEL", "find_bleed",
           "is_shared_room", "text_similarity", "track_speaker", "transcribe_meeting"]
