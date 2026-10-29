"""Transcribe a meeting while it runs.

A meeting's tracks (one per participant, ``meeting_tracks``) are transcribed window by
window on the worker's existing ticks while the bot is still uploading, so when the
meeting ends only the last window of each track and the summary are left.

Nothing new runs for it: no process, thread, timer or poller. ``Worker.tick`` calls
``Pipeline.transcribe_live``, which asks the store one indexed question (is a meeting
being recorded?) and imports this module only when the answer is yes.

**Same result as transcribing at the end.** The end-of-meeting pass
(``meeting_tracks.transcribe_meeting``) runs the same code: it finishes each track's
state here, starting from nothing when there is none (``CONVO_MEETING_LIVE_TRANSCRIBE``
off). Live work only does what that pass would do, earlier:

* **Runs** are a track's contiguous chunks, as in ``Pipeline._runs``. While recording,
  only the gap-free sequence prefix counts (a missing chunk may still arrive). A run is
  closed once the next chunk of the track has arrived and doesn't continue it.
* **Windows** are planned from the stored VAD spans and chunk lengths with
  ``Pipeline._plan_windows``, without decoding audio. Planning is greedy, so every window
  but the last of an open run is final: a window is cut only when the next speech region
  doesn't fit, and audio that arrives later can only make that region longer. Those
  windows are exactly the ones the end-of-meeting plan has, so whisper hears the same
  clips (sliced at the same run samples, ``RunAudio``) with the same context: a word that
  crosses a chunk boundary is in one window, never cut. The last window of an open run
  waits for more audio or for the end.
* **Context**: whisper's context words carry on per track across windows and runs.
* **Pieces, not joined lines**: the state keeps whisper's own pieces, each with its
  loudness (dBFS on its own track, for the bleed check) measured from the same samples
  the end pass would read. Bleed is decided at the end over all tracks' pieces (text and
  stored loudness only), and only then are lines joined (``CONVO_JOIN_LINES``), as
  ``meeting_tracks`` does for every track (QA BUG-04).
* **Prompt**: the participants' names whisper is given are those of the tracks known at
  the time. Someone who joins late is only in the prompt of later windows; that is the
  one way a live result can differ from an end-only one (spelling of a name).

**Deferred to the end**: shared-room tracks (any display name they ever had is a room
or a bare device, by the end pass's own rule, ``meeting_tracks.is_room_track``: diarized
over the whole track so its "Speaker N" numbering is consistent), tracks with no display
name yet, and chunks without a track.

**Crash-safe**: each window's result is saved (``meeting_live`` table) with how far the
track got; a restart redoes at most the window in flight. The state names the runs and
the window it covers and is checked against the chunks before use: anything that no
longer matches (a late chunk changed a run) is thrown away and that track is
transcribed again from its audio, which is kept until the end-of-meeting pass succeeds.

Imported only when a meeting is being recorded or processed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ...audio import SAMPLE_RATE, AudioDecodeError
from ...clips import Window
from ...diarize import PieceRef, rms_db
from ...transcribe_engine import load_chunk, plan_windows, whisper_window
from ...vad import Span
from ..people.names import clean_display_name
from .tracks import is_room_track, is_shared_room, meeting_prompt

if TYPE_CHECKING:  # pragma: no cover
    from ...pipeline import Pipeline

log = logging.getLogger("athena.meeting_live")  # its name before the move

# A line: (epoch start, epoch end, text, PieceRef(run = first sequence of its run, run
# time start, run time end)) plus its loudness in dBFS.
Piece = tuple[float, float, str, PieceRef]


class LiveMismatch(Exception):
    """A chunk's audio is not as long as its stored duration."""


# A chunk's stored duration is its decoded size / SAMPLE_RATE; a float rounded on the way
# can read one sample more or less. That much is the same audio (padded or cut by one
# sample), not a mismatch (QA BUG-03).
SIZE_TOLERANCE = 1
# What reading a chunk's audio can fail with: the file gone or unreadable, audio that
# doesn't decode, or audio not as long as stored.
UNREADABLE = (OSError, AudioDecodeError, LiveMismatch)


@dataclass
class Run:
    """Contiguous chunks of one track (``Pipeline._runs``); ``closed`` when no later chunk
    can extend it."""
    chunks: list[dict[str, Any]]
    closed: bool

    @property
    def first(self) -> int:
        return self.chunks[0]["sequence"]

    @property
    def last(self) -> int:
        return self.chunks[-1]["sequence"]

    @property
    def sizes(self) -> list[int]:
        """Samples per chunk (the stored duration is the decoded size / SAMPLE_RATE)."""
        return [round(c["duration"] * SAMPLE_RATE) for c in self.chunks]

    def windows(self, pipeline: Pipeline) -> list[Window]:
        """All whisper windows of the run so far, planned as ``Pipeline._whisper_run``
        plans them (same spans, same offsets, same run length)."""
        spans: list[Span] = []
        offset = 0.0
        for c, n in zip(self.chunks, self.sizes, strict=True):
            spans += [Span(offset + s, offset + e) for s, e in c["speech"]]
            offset += n / SAMPLE_RATE
        return plan_windows(pipeline.settings, spans, sum(self.sizes) / SAMPLE_RATE)

    def final_windows(self, pipeline: Pipeline) -> list[Window]:
        """The windows no later audio can change: all of a closed run, all but the last
        of an open one."""
        ws = self.windows(pipeline)
        return ws if self.closed else ws[:-1]


def plan_runs(chunks: list[dict[str, Any]], *, final: bool) -> list[Run]:
    """A track's runs of contiguous chunks, as ``Pipeline._runs`` cuts them. While the
    meeting records (``final`` false) only the gap-free sequence prefix is used and the
    last run stays open."""
    from ...transcribe_engine import CONTIGUOUS_TOLERANCE

    ordered = sorted((c for c in chunks if c["audio_path"]), key=lambda c: c["sequence"])
    if not final:
        for i in range(1, len(ordered)):
            if ordered[i]["sequence"] != ordered[i - 1]["sequence"] + 1:
                ordered = ordered[:i]
                break
    runs: list[Run] = []
    cur: list[dict[str, Any]] = []
    for c in ordered:
        if cur:
            prev = cur[-1]
            gap = c["started_at"] - (prev["started_at"] + prev["duration"])
            if abs(gap) > CONTIGUOUS_TOLERANCE or c["sequence"] != prev["sequence"] + 1:
                runs.append(Run(cur, closed=True))
                cur = []
        cur.append(c)
    if cur:
        runs.append(Run(cur, closed=final))
    return runs


class RunAudio:
    """A run's audio, decoded chunk by chunk only where it is read. ``slice(a, b)`` equals
    ``run_audio[a:b]`` of the whole decoded run, sample for sample."""

    def __init__(self, pipeline: Pipeline, run: Run) -> None:
        self.pipeline = pipeline
        self.run = run
        self.sizes = run.sizes
        self.offsets = [0]
        for n in self.sizes:
            self.offsets.append(self.offsets[-1] + n)
        self._pcm: dict[int, np.ndarray] = {}

    def _chunk(self, i: int) -> np.ndarray:
        if i not in self._pcm:
            pcm = load_chunk(self.run.chunks[i])
            n = self.sizes[i]
            if abs(pcm.size - n) > SIZE_TOLERANCE:
                raise LiveMismatch(f"chunk {self.run.chunks[i]['sequence']}: "
                                   f"{pcm.size} samples, expected {n}")
            if pcm.size != n:
                pcm = pcm[:n] if pcm.size > n else np.pad(pcm, (0, n - pcm.size))
            self._pcm[i] = pcm
        return self._pcm[i]

    def slice(self, a: int, b: int) -> np.ndarray:
        parts = []
        for i, n in enumerate(self.sizes):
            off = self.offsets[i]
            lo, hi = max(a, off), min(b, off + n)
            if lo < hi:
                parts.append(self._chunk(i)[lo - off:hi - off])
        if not parts:
            return np.zeros(0, np.float32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def extract(self, w: Window) -> np.ndarray:
        """``w.extract(run_audio)``."""
        slices = [self.slice(int(a * SAMPLE_RATE), int(b * SAMPLE_RATE)) for a, b in w.parts]
        return slices[0] if len(slices) == 1 else np.concatenate(slices)

    def loudness(self, ref: PieceRef) -> float:
        """dBFS of a line's audio, as ``meeting_tracks`` measures it on the whole run."""
        return rms_db(self.slice(int(ref.start * SAMPLE_RATE), int(ref.end * SAMPLE_RATE)))


@dataclass
class TrackState:
    """How far a track is transcribed. ``runs_done``: [first, last] sequence of each run
    finished; ``run`` / ``windows`` / ``until``: the run in progress (its first
    sequence), how many of its windows are done and where the last one ends (run time,
    a check); ``lines`` whisper's pieces so far, unjoined, each [start, end, text, run,
    ref_start, ref_end, loudness]. A state saved by an older version (with a joined
    ``open`` line) doesn't load, so that track starts again."""
    runs_done: list[list[int]] = field(default_factory=list)
    run: int | None = None
    windows: int = 0
    until: float = 0.0
    context: str = ""
    lines: list[list[Any]] = field(default_factory=list)

    @classmethod
    def load(cls, raw: str | None) -> TrackState | None:
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (TypeError, ValueError):
            return None

    def dump(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    def empty(self) -> bool:
        return not (self.runs_done or self.run is not None or self.lines)

    def matches(self, runs: list[Run], pipeline: Pipeline) -> bool:
        """Whether this state still describes ``runs``: the same finished runs, and the
        run in progress starts where it did with the same windows done."""
        k = len(self.runs_done)
        if k > len(runs):
            return False
        for (first, last), r in zip(self.runs_done, runs, strict=False):
            if (first, last) != (r.first, r.last) or not r.closed:
                return False
        if self.run is None:
            return self.windows == 0
        if k >= len(runs) or runs[k].first != self.run:
            return False
        if self.windows:
            done = runs[k].final_windows(pipeline)
            if len(done) < self.windows or done[self.windows - 1].parts[-1][1] != self.until:
                return False
        return True

    def all_lines(self) -> list[tuple[Piece, float]]:
        return [((r[0], r[1], r[2], PieceRef(r[3], r[4], r[5])), r[6]) for r in self.lines]


def _row(p: Piece, loud: float) -> list[Any]:
    s, e, text, ref = p
    return [s, e, text, ref.run, ref.start, ref.end, loud]


def track_steps(pipeline: Pipeline, session_id: str, track: str, runs: list[Run],
                st: TrackState, prompt: Callable[[], str]) -> Iterator[None]:
    """Transcribe the track's final windows not done yet, one window per step, saving
    the state after each; ``prompt`` gives whisper's prompt (asked once, when needed)."""
    store = pipeline.store
    text_prompt: str | None = None
    for run in runs[len(st.runs_done):]:
        if st.run != run.first:
            st.run, st.windows, st.until = run.first, 0, 0.0
        todo = run.final_windows(pipeline)
        audio = RunAudio(pipeline, run)
        start = run.chunks[0]["started_at"]
        while st.windows < len(todo):
            w = todo[st.windows]
            if text_prompt is None:
                text_prompt = prompt()
            got, _, context = whisper_window(pipeline.settings, pipeline.transcriber,
                                             run.first, start, w, audio.extract(w),
                                             text_prompt, st.context)
            got.sort(key=lambda p: p[0])  # as the end pass sorts the track's pieces
            _fold(st, got, audio)
            st.windows, st.until, st.context = st.windows + 1, w.parts[-1][1], context
            store.put_live_state(session_id, track, st.dump())
            yield
        if not run.closed:
            return  # its last window waits for more audio or the end of the meeting
        st.runs_done.append([run.first, run.last])
        st.run, st.windows, st.until = None, 0, 0.0
        store.put_live_state(session_id, track, st.dump())


def _fold(st: TrackState, got: list[Piece], audio: RunAudio) -> None:
    """Add a window's pieces (in time order) to the track's pieces, each measured.
    Nothing is joined here: bleed is decided on the unjoined pieces at the end, then
    ``meeting_tracks`` joins the lines kept (QA BUG-04)."""
    st.lines += [_row(p, audio.loudness(p[3])) for p in got]


def deferred(info: dict[str, Any], owner_names: list[str]) -> bool:
    """A track left to the end-of-meeting pass: a shared room by the rule that pass uses
    (``meeting_tracks.is_room_track``: any display name it ever had is a room or a
    device, owner names excepted; QA BUG-01/15), so it is diarized over the whole track
    and its "Speaker N" numbering stays consistent. A track with no display name yet
    waits too: its first chunk can arrive before the name, which may be a room's
    (QA BUG-24)."""
    from ..people.names import _track_names

    return not _track_names(info) or is_room_track(info, owner_names)


def meeting_whisper_prompt(pipeline: Pipeline, infos: list[dict[str, Any]]) -> str:
    """Whisper's prompt for a meeting: the usual one plus the participants' display names
    (cleaned, rooms and devices left out), in track order. Read-only, so it is the same
    while the meeting runs and at its end."""
    from ...pipeline import MAX_PROMPT_NAMES

    prompt = pipeline.whisper_prompt()
    if not pipeline.settings.whisper_prompt_names:
        return prompt
    names = []
    for t in infos:
        c = clean_display_name(t.get("name") or "")
        if c and not is_shared_room(c):
            names.append(c)
    return meeting_prompt(prompt, names, MAX_PROMPT_NAMES)


def finish_track(pipeline: Pipeline, session_id: str, track: str,
                 chunks: list[dict[str, Any]], raw_state: str | None,
                 prompt: Callable[[], str]) -> list[tuple[Piece, float]]:
    """The end-of-meeting pass for one track: what live transcription did, plus the
    windows left. Without a usable state the whole track is transcribed here.

    A chunk that can't be read as stored (file gone or corrupt, audio not as long as its
    duration) doesn't fail the meeting (QA BUG-03): the track's live work is dropped and
    the track is transcribed again from the chunks that read, each as long as its
    audio."""
    try:
        return _finish_track(pipeline, session_id, track, chunks, raw_state, prompt)
    except UNREADABLE as e:
        log.warning("meeting %s track %s: %s; transcribing the track again from the "
                    "chunks that read", session_id, track, e)
    pipeline.store.put_live_state(session_id, track, TrackState().dump())
    return _finish_track(pipeline, session_id, track, _readable(pipeline, chunks), None,
                         prompt)


def _readable(pipeline: Pipeline, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chunks whose audio decodes, each with the duration of its audio."""
    out = []
    for c in chunks:
        if not c["audio_path"]:
            continue
        try:
            pcm = load_chunk(c)
        except (OSError, AudioDecodeError) as e:
            log.warning("chunk %s of track %s left out: %s", c["sequence"], c["track"], e)
            continue
        out.append({**c, "duration": pcm.size / SAMPLE_RATE})
    return out


def _finish_track(pipeline: Pipeline, session_id: str, track: str,
                  chunks: list[dict[str, Any]], raw_state: str | None,
                  prompt: Callable[[], str]) -> list[tuple[Piece, float]]:
    runs = plan_runs(chunks, final=True)
    st = TrackState.load(raw_state) or TrackState()
    if not st.matches(runs, pipeline):
        log.warning("meeting %s track %s: live transcript no longer matches its chunks; "
                    "transcribing the track again", session_id, track)
        st = TrackState()
    for _ in track_steps(pipeline, session_id, track, runs, st, prompt):
        pass
    return st.all_lines()


def step(pipeline: Pipeline, session_ids: list[str], budget_seconds: float,
         clock: Callable[[], float] = time.monotonic) -> int:
    """One worker tick of live work on the meetings being recorded: windows of their
    tracks one at a time, round robin over tracks, until nothing is ready or about
    ``budget_seconds`` have passed (at least one window). Returns windows done."""
    deadline = clock() + max(0.0, budget_seconds)
    steps: list[Iterator[None]] = []
    for sid in session_ids:
        steps += _session_steps(pipeline, sid)
    done = 0
    try:
        while steps:
            for it in list(steps):
                if done and clock() >= deadline:
                    return done
                try:
                    next(it)
                    done += 1
                except StopIteration:
                    steps.remove(it)
                except UNREADABLE as e:
                    # e.g. a chunk whose file is still being moved in place: next tick
                    log.info("live transcription paused for a track: %s", e)
                    steps.remove(it)
        return done
    finally:
        for it in steps:
            it.close()


def _session_steps(pipeline: Pipeline, session_id: str) -> list[Iterator[None]]:
    store = pipeline.store
    sess = store.get_session(session_id)
    if sess is None or sess["status"] != "recording":
        return []
    infos = store.tracks(session_id)
    owners = pipeline.settings.zoom_owner_name_list
    states = store.live_states(session_id)
    by_track: dict[str, list[dict[str, Any]]] = {}
    for c in store.chunks(session_id):
        if c["track"]:
            by_track.setdefault(c["track"], []).append(c)
    cache: list[str] = []

    def prompt() -> str:
        if not cache:
            cache.append(meeting_whisper_prompt(pipeline, infos))
        return cache[0]

    out = []
    for info in infos:
        track = info["track"]
        if deferred(info, owners) or track not in by_track:
            continue
        runs = plan_runs(by_track[track], final=False)
        st = TrackState.load(states.get(track)) or TrackState()
        if not st.matches(runs, pipeline):
            if not st.empty():
                log.info("meeting %s track %s: chunks changed; live transcript restarted",
                         session_id, track)
            st = TrackState()
            store.put_live_state(session_id, track, st.dump())
        out.append(track_steps(pipeline, session_id, track, runs, st, prompt))
    return out
