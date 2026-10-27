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
* **Shared rooms and devices**: a track whose display name is, or ever was, a room,
  conference hardware or a bare device with no person ("Conference Room", "Polycom",
  "iPhone", "Zoom user", ``is_shared_room``; "Kath's iPhone" is Kath) can hold
  several people. The configured diarizer runs on that track only; its unnamed voices become
  "Speaker N" (numbered across the meeting) with stored voices, so ``name_speaker`` and
  ``list_unnamed_speakers`` work on them as on any recording.
* **Owner**: a track named in ``CONVO_ZOOM_OWNER_NAMES`` is the owner's
  (``is_owner=true``), named as the owner is everywhere else.

A person's track is finished from what ``meeting_live`` (lane 5D) transcribed while the
meeting ran, plus the windows left; it gives the same lines as transcribing it here.

Imported only by ``Pipeline._transcribe_session`` for a session that has tracks and by
``meeting_live`` while a meeting records, so a server that never sees a meeting never
loads this module.
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
from .diarize import PieceRef, SpeakerLabel, load_voices, rms_db
from .lines import join_lines
from .store import Segment

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline
    from .store import Store

log = logging.getLogger(__name__)

# Bleed: a line is a copy of a line on another track when its words match that line's
# (``text_similarity``: the better of SequenceMatcher's ratio over words and, when the
# shorter line has at least BLEED_CONTAINED_MIN_WORDS words, the share of its words
# found in the other line in order) ...
# ... at least this well, however loud the two are (a word or so off);
BLEED_SURE_SIMILARITY = 0.9
# ... or at least this well when it is also BLEED_MIN_DB quieter than the other line.
# The quieter mic hears the voice from across the room, so whisper often slips a word
# there ("ship the release of Friday"); two people saying nearly the same sentence at
# the same moment are about as loud as each other, and both lines stay.
BLEED_MIN_SIMILARITY = 0.7
BLEED_MIN_DB = 6.0
BLEED_CONTAINED_MIN_WORDS = 4
# ... and the two lines overlap in time by at least this share of the shorter line
# (whisper's line edges differ a little between the two copies). A line shorter than
# BLEED_MIN_SECONDS counts as that long.
BLEED_MIN_OVERLAP = 0.5
BLEED_MIN_SECONDS = 0.5

_UNNAMED = re.compile(r"^Speaker \d+$")
_WORD = re.compile(r"[\w']+")

def is_shared_room(name: str | None) -> bool:
    """True when a Zoom display name can stand for several people: a room or conference
    hardware ("Conference Room", "Polycom", "Huddle 4", "Rally Bar"), a bare device with
    no person on it ("iPhone", "Galaxy S23"), a dial-in number or "Zoom user". Not a
    person whose name holds such a word ("Jordan Hall", "Cisco Ramirez") or a personal
    device ("Kath's iPhone" is Kath). Lane 2B's ``meeting_names.is_room_or_hardware`` is
    the one rule; this name stays for callers of lane 2A's API."""
    from .meeting_names import is_room_or_hardware

    return is_room_or_hardware(name)


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def track_speaker(store: Store, track_info: dict[str, Any],
                  owner_names: Sequence[str]) -> tuple[str, bool]:
    """Who a track's lines belong to: (speaker name for its segments, is_owner).

    The one place a track becomes a speaker. Lane 2B's ``meeting_names.speaker_for_track``
    cleans the display name ("Anna (she/her) | Acme" -> "Anna"), matches it against people
    and their aliases (creating a person when it is new) and keeps every display name as
    an alias. An empty name means the track has no person's name (a device, a room or
    nothing); the caller numbers it "Speaker N" or diarizes it as a shared room.
    """
    from .meeting_names import speaker_for_track

    return speaker_for_track(store, {**track_info, "placeholder": ""}, owner_names)


def _room_track(info: dict[str, Any], owner_names: Sequence[str]) -> bool:
    """True when any display name the track ever had (raw) is a room or a device, other
    than one of the owner's own names ("Cleo (phone)"). A track that showed "Conference
    Room 2" and later "Alice Example" stays a room: most likely someone in that room set
    a name, and others may still be speaking there. Diarizing is the safe side: the
    diarizer still names enrolled voices, while a wrongly named track would put a whole
    room's words on one person."""
    names = list(info.get("names") or [])
    if info.get("name") and info["name"] not in names:
        names.append(info["name"])
    return any(is_shared_room(n) and not any(_same_name(n, o) for o in owner_names
                                             if o.strip())
               for n in names)


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
    """How nearly two lines say the same words, 0..1 (see ``find_bleed``)."""
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
    the same words: similarity >= BLEED_SURE_SIMILARITY, or >= BLEED_MIN_SIMILARITY
    with one line at least BLEED_MIN_DB quieter. The quieter one is dropped
    (``loudness``: dBFS of each line's audio on its own track; a tie keeps the line with
    more words). Best matches are decided first, and a dropped line never causes
    another drop.
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
            if sim >= BLEED_SURE_SIMILARITY or (
                    sim >= BLEED_MIN_SIMILARITY
                    and abs(loudness[i] - loudness[j]) >= BLEED_MIN_DB):
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
    track (shared-room tracks by the diarizer), bleed dropped, merged in time order.

    A track of a person is finished from what ``meeting_live`` transcribed while the
    meeting ran (the same windows, context and pieces this pass would make), plus the
    windows left; without live work it is transcribed here from the start. Shared-room
    tracks and chunks without a track are always transcribed here, whole."""
    from . import meeting_live

    store, cfg = pipeline.store, pipeline.settings
    owner_names = cfg.zoom_owner_name_list
    track_list = store.tracks(session_id)
    infos = {t["track"]: t for t in track_list}
    states = store.live_states(session_id)
    by_track: dict[str, list[dict[str, Any]]] = {}
    for c in chunks:
        by_track.setdefault(c.get("track") or "", []).append(c)
    # tracks in order of first audio; chunks without a track (none from the bot) last
    order = sorted(by_track, key=lambda t: (t == "", infos.get(t, {}).get("started_at", 0.0),
                                            t))
    who = {t: track_speaker(store, infos.get(t, {"track": t, "name": None}), owner_names)
           for t in order if t}
    # after naming the tracks: an owner track matched by CONVO_ZOOM_OWNER_NAMES makes the
    # owner's person when no owner voice is enrolled, and the lines take its name (QA BUG-07)
    owner_display = store.owner_name() or cfg.owner_name
    prompt = meeting_live.meeting_whisper_prompt(pipeline, track_list)

    lines: list[Line] = []
    loud: list[float] = []  # dBFS of each line on its own track, for the bleed check
    candidates: dict[str, np.ndarray] = {}
    unnamed = 0  # "Speaker N" numbering across the meeting
    voices_loaded = False
    for track in order:
        name, owner = who.get(track, ("", False))
        # a shared room: any display name the track ever had is a room or a device
        # ("Conference Room 2", "iPhone", "Zoom user"), other than the owner's own names
        raw = infos.get(track, {}).get("name")
        shared = not track or (not owner and _room_track(infos.get(track, {}), owner_names))
        if not shared:
            got = meeting_live.finish_track(pipeline, session_id, track, by_track[track],
                                            states.get(track), lambda: prompt)
            if not got:
                continue
            if owner:
                name = owner_display or name
            elif not name:
                unnamed += 1
                name = f"Speaker {unnamed}"
            label = SpeakerLabel(name, owner)
            lines += [Line(s, e, text, ref, track, label) for (s, e, text, ref), _ in got]
            loud += [db for _, db in got]
            continue
        runs: list[np.ndarray] = []
        pieces: list[tuple[float, float, str, PieceRef]] = []
        words: list[list[tuple[float, float, str]]] = []
        context = ""  # whisper's context starts again on every track
        for run_start, audio, speech in pipeline._runs(by_track[track]):
            runs.append(audio)
            got_pieces, got_words, context = pipeline._whisper_run(
                len(runs) - 1, run_start, audio, speech, prompt, context)
            pieces += got_pieces
            words += got_words
        if not pieces:
            continue
        idx = sorted(range(len(pieces)), key=lambda i: pieces[i][0])
        pieces, words = [pieces[i] for i in idx], [words[i] for i in idx]
        if pipeline.diarizer is not None:
            if not voices_loaded:
                pipeline.diarizer.voices = load_voices(store)
                voices_loaded = True
            labels, found, pieces = _diarize_track(pipeline, runs, 0, pieces, words)
            renumber: dict[str, str] = {}
            for lab in labels:
                if _UNNAMED.match(lab.name) and lab.name not in renumber:
                    unnamed += 1
                    renumber[lab.name] = f"Speaker {unnamed}"
            labels = [SpeakerLabel(renumber.get(lab.name, lab.name), lab.is_owner)
                      for lab in labels]
            candidates.update({renumber[k]: v for k, v in found.items() if k in renumber})
        else:
            if not name and raw:  # a room, diarization off: the room's name
                name = " ".join(raw.split())
            elif not name:
                unnamed += 1
                name = f"Speaker {unnamed}"
            labels = [SpeakerLabel(name, False)] * len(pieces)
        # Bleed is decided on whisper's own pieces, before any joining: a joined line
        # spans its speaker's pauses, so another track's words said in such a pause would
        # look simultaneous, and the pause's silence would lower the line's loudness.
        for (s, e, text, ref), lab in zip(pieces, labels, strict=True):
            lines.append(Line(s, e, text, ref, track, lab))
            loud.append(rms_db(runs[ref.run][int(ref.start * SAMPLE_RATE):
                                             int(ref.end * SAMPLE_RATE)]))
    if pipeline.diarizer is not None and voices_loaded:
        store.put_speaker_candidates(session_id, candidates)

    drop = find_bleed(lines, loud)
    kept_lines = [ln for i, ln in enumerate(lines) if i not in drop]
    if cfg.join_lines:  # then join, within each track only: never two participants
        kept_lines = [j for track in order
                      for j in _join_track([ln for ln in kept_lines if ln.track == track],
                                           cfg.join_gap_seconds, cfg.join_max_seconds)]
    rank = {t: i for i, t in enumerate(order)}
    kept = sorted(kept_lines, key=lambda ln: (ln.start, rank[ln.track], ln.end))
    return [Segment(started_at=ln.start, ended_at=ln.end, text=ln.text,
                    speaker=ln.label.name, is_owner=ln.label.is_owner) for ln in kept]


def _join_track(lines: list[Line], gap: float, max_seconds: float) -> list[Line]:
    """``join_lines`` over one track's kept lines (in time order); a joined line spans
    from its first piece's start to its last piece's end."""
    if not lines:
        return lines
    track = lines[0].track
    pieces, labels = join_lines([(ln.start, ln.end, ln.text, ln.ref) for ln in lines],
                                [ln.label for ln in lines], gap, max_seconds)
    assert labels is not None
    return [Line(s, e, text, ref, track, lab)
            for (s, e, text, ref), lab in zip(pieces, labels, strict=True)]


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

