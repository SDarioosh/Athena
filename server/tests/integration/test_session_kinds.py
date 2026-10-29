"""Characterization of how recordings become conversations, by session kind.

A table of fictional sessions goes through the real ``Pipeline`` (fake VAD-friendly
tones, the fake whisper, the tone "voice" embedder and the fake llama-server of
``tests/conftest.py``) with a fixed clock and fixed conversation ids. Each scenario
records, step by step, the ingest answers (``ok``, ``stop``, ``reason``, ...), what each
step returned, every summary request (its schema and a hash of its messages, plus the
start of the transcript the model was given) and, at the end, every stored row (the
search index's internal tables left out). ``snapshots/session_kinds/<scenario>.json``
holds the result of the code before session kinds were introduced; any difference fails
with a diff. Regenerate only when a change of behaviour is intended:

    UPDATE_SNAPSHOTS=1 pytest tests/integration/test_session_kinds.py

Kinds covered: plain walk-around recordings (gaps under and over
``CONVO_CONVERSATION_GAP_SECONDS``, a new session within the gap on the same device,
other devices, a session bridging two conversations), silence and max-duration stops,
retries, idle sessions, failed transcription, voice notes, notes written by hand,
meetings with tracks (per-track sequences, late chunks, chat, never split, never joined,
live transcription), chat-only Zoom sessions, enrollment recordings, and the two
ingest/finalize races (``_late_meeting_chunk`` and ``MeetingClosed``) and the
mid-processing ones.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from athena import voice_enroll
from athena.config import Settings
from athena.db import conversations as conversations_module
from athena.db.store import Store
from athena.diarize import Diarizer, load_voices
from athena.pipeline import Pipeline
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from athena.vad import EnergyVAD
from tests.conftest import (
    SAMPLE_RATE,
    TOKEN,
    FakeTranscriber,
    ToneEmbedder,
    fake_llm_transport,
    silence,
    tone,
    wav,
)

SNAPSHOTS = Path(__file__).parent / "snapshots" / "session_kinds"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC


# ------------------------------------------------------------------ harness
class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def chunk(*parts: tuple[float, np.ndarray], total: float = 30.0) -> bytes:
    """``total`` seconds of silence with each (offset, audio) part placed in it."""
    out = np.zeros(int(total * SAMPLE_RATE), np.float32)
    for at, p in parts:
        i = int(at * SAMPLE_RATE)
        out[i:i + p.size] += p
    return wav(out)


SPEECH = chunk((2, tone(200, 6)))           # the owner ("Me", enrolled at 200 Hz)
OTHER = chunk((3, tone(600, 6)))            # someone else
QUIET = chunk()                             # 30 s of silence


class Run:
    """One scenario: a pipeline on a fresh store, a fixed clock, and the record."""

    def __init__(self, tmp: Path, mp: pytest.MonkeyPatch, **overrides: Any) -> None:
        self.clock = Clock(T0)
        mp.setattr(time, "time", self.clock)
        counter = itertools.count(1)
        mp.setattr(conversations_module, "uuid", SimpleNamespace(
            uuid4=lambda: SimpleNamespace(hex=f"{0xC0DE0000 + next(counter):032x}")))
        opts: dict[str, Any] = {
            "ingest_token": TOKEN, "data_dir": tmp / "data", "silence_stop_seconds": 60,
            "max_session_seconds": 600, "session_idle_seconds": 120, "run_worker": False,
            "vad_backend": "energy", "whisper_window_seconds": 0, "timezone": "UTC",
            "zoom_owner_names": "Cleo Example"}
        opts.update(overrides)
        self.settings = Settings(_env_file=None, **opts)
        self.store = Store(self.settings.db_path)
        self.llm_log: list[dict[str, Any]] = []
        llm = LLMClient("http://llm.test/v1", "test-model",
                        transport=fake_llm_transport(log=self.llm_log))
        self.store.put_speaker("Me", ToneEmbedder().embed(tone(200, 2)), is_owner=True)
        diarizer = Diarizer(ToneEmbedder(), load_voices(self.store))
        self.transcriber = FakeTranscriber()
        self.p = Pipeline(self.settings, self.store, EnergyVAD(), self.transcriber,
                          diarizer, Summarizer(llm, "Me"))
        self.steps: list[dict[str, Any]] = []
        self._llm_seen = 0

    # -- steps ----------------------------------------------------------------
    def _record(self, step: str, out: Any) -> Any:
        calls = self.llm_log[self._llm_seen:]
        self._llm_seen = len(self.llm_log)
        entry: dict[str, Any] = {"step": step, "out": _plain(out)}
        if calls:
            entry["llm"] = [_llm_call(c) for c in calls]
        self.steps.append(entry)
        return out

    def ingest(self, session: str, seq: int, start: float, data: bytes, *,
               device: str = "omi", track: str | None = None, name: str | None = None,
               kind: str = "conversation", arrive: float | None = None) -> dict[str, Any]:
        """Upload one chunk ``start`` seconds after T0; it arrives when it ends (or at
        ``arrive``)."""
        self.clock.now = T0 + (start + 30 if arrive is None else arrive)
        label = f"ingest {session} {track or '-'}#{seq} @{start:g}"
        try:
            res = self.p.ingest_chunk(device=device, session_id=session, sequence=seq,
                                      started_at=T0 + start, codec="wav", data=data,
                                      now=self.clock.now, kind=kind, track=track,
                                      track_name=name).as_dict()
        except ValueError as e:
            res = {"error": f"ValueError: {e}"}
        return self._record(label, res)

    def end(self, session: str) -> Any:
        return self._record(f"end {session}", self.p.end_session(session))

    def process(self, at: float | None = None) -> Any:
        if at is not None:
            self.clock.now = T0 + at
        return self._record("process_pending", self.p.process_pending(now=self.clock.now))

    def process_session(self, session: str) -> Any:
        return self._record(f"process_session {session}", self.p.process_session(session))

    def idle(self, at: float) -> Any:
        self.clock.now = T0 + at
        return self._record("finalize_idle", self.p.finalize_idle(now=self.clock.now))

    def live(self) -> Any:
        return self._record("transcribe_live", self.p.transcribe_live())

    def chat(self, session: str, body: dict[str, Any]) -> Any:
        from athena.meetings.ingest.chat import ChatIn, NotAMeeting, ingest_chat

        try:
            out = ingest_chat(self.p, session, ChatIn.model_validate(body))
        except NotAMeeting as e:
            out = {"error": f"NotAMeeting: {e}"}
        return self._record(f"chat {session}", out)

    def text(self, source_id: str, text: str, written_at: float, **kw: Any) -> Any:
        return self._record(f"ingest_text {source_id}", self.p.ingest_text(
            source_id=source_id, text=text, written_at=T0 + written_at,
            link=f"https://hub.example/notes/{source_id}", now=self.clock.now, **kw))

    def delete_text(self, source_id: str) -> Any:
        return self._record(f"delete_text {source_id}", self.p.delete_text(source_id))

    def note(self, label: str, value: Any) -> Any:
        return self._record(label, value)

    # -- result -----------------------------------------------------------------
    def result(self) -> dict[str, Any]:
        return {"steps": self.steps, "rows": dump_rows(self.store),
                "audio_files": sorted(str(p.relative_to(self.settings.audio_dir))
                                      for p in self.settings.audio_dir.rglob("*")
                                      if p.is_file())
                if self.settings.audio_dir.exists() else []}


def _plain(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, bytes | memoryview):
        return "sha256:" + hashlib.sha256(bytes(value)).hexdigest()[:16]
    return value


def _llm_call(body: dict[str, Any]) -> dict[str, Any]:
    messages = json.dumps(body["messages"], sort_keys=True, ensure_ascii=False)
    user = body["messages"][-1]["content"]
    return {"schema": body["response_format"]["json_schema"]["name"],
            "messages_sha256": hashlib.sha256(messages.encode()).hexdigest(),
            "user_head": user[:300]}


def dump_rows(store: Store) -> dict[str, list[Any]]:
    """Every row of every table but the full-text indexes' internals, as plain values in
    a stable order. ``audio_path`` keeps only its file name (the folder is a temp dir)."""
    out: dict[str, list[Any]] = {}
    conn = store._conn
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    for name in names:
        if "_fts_" in name or name.endswith("_fts") and name != "search_fts":
            continue
        cur = conn.execute(f"SELECT * FROM {name}")  # noqa: S608 - table names from sqlite
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r, strict=True))
            if row.get("audio_path"):
                row["audio_path"] = Path(row["audio_path"]).name
            rows.append(_plain(row))
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
        if rows:
            out[name] = rows
    return out


def check(name: str, value: dict[str, Any]) -> None:
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    path = SNAPSHOTS / f"{name}.json"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no snapshot {path.name}; run with UPDATE_SNAPSHOTS=1"
    old = path.read_text(encoding="utf-8")
    if old != text:
        diff = "".join(itertools.islice(difflib.unified_diff(
            old.splitlines(True), text.splitlines(True), "snapshot", "now"), 200))
        pytest.fail(f"{name} changed:\n{diff}")


# ------------------------------------------------------------- plain sessions
def plain_gap_over(r: Run) -> None:
    """Speech, 5 min of silence, speech: two conversations."""
    r.ingest("s1", 0, 0, chunk((0, tone(200, 4)), (8, tone(600, 4))))
    for seq in range(1, 11):
        r.ingest("s1", seq, 30 * seq, QUIET, arrive=30 * seq + 1)  # never 60 s quiet
    r.ingest("s1", 11, 330, OTHER)
    r.end("s1")
    r.process()


def plain_gap_under(r: Run) -> None:
    """Speech, 2 min of silence, speech: one conversation."""
    r.ingest("s1", 0, 0, SPEECH)
    for seq in range(1, 4):
        r.ingest("s1", seq, 30 * seq, QUIET, arrive=30 * seq + 1)
    r.ingest("s1", 4, 120, OTHER)
    r.end("s1")
    r.process()


def plain_next_session_within_gap(r: Run) -> None:
    """A new session on the same device 60 s later extends the conversation."""
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    r.process()
    r.ingest("s2", 0, 60, OTHER)
    r.end("s2")
    r.process()


def plain_next_session_beyond_gap(r: Run) -> None:
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    r.process()
    r.ingest("s2", 0, 400, OTHER)
    r.end("s2")
    r.process()


def plain_other_device(r: Run) -> None:
    """Two devices at the same time: two conversations."""
    r.ingest("s1", 0, 0, SPEECH, device="omi")
    r.ingest("s2", 0, 10, OTHER, device="trex3pro")
    r.end("s1")
    r.end("s2")
    r.process()


def plain_bridge_absorbs(r: Run) -> None:
    """A session uploaded late between two conversations of one device joins both."""
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    r.ingest("s3", 0, 400, OTHER)
    r.end("s3")
    r.process()
    r.ingest("s2", 0, 200, SPEECH, arrive=900)
    r.end("s2")
    r.process()


def plain_silence_stop(r: Run) -> None:
    """Trailing silence >= 60 s stops; a chunk after the stop is told so again."""
    r.ingest("s1", 0, 0, SPEECH)
    r.ingest("s1", 1, 30, QUIET)
    r.ingest("s1", 2, 60, QUIET)
    r.ingest("s1", 3, 90, QUIET)
    r.ingest("s1", 4, 120, SPEECH)
    r.process()


def plain_max_duration(r: Run) -> None:
    for seq in range(0, 22):
        r.ingest("s1", seq, 30 * seq, SPEECH if seq % 2 == 0 else OTHER)
    r.process()


def plain_retry(r: Run) -> None:
    """The same (session, sequence) twice: one chunk, ``duplicate: true``."""
    r.ingest("s1", 0, 0, SPEECH)
    r.ingest("s1", 0, 0, SPEECH)
    r.ingest("s1", 0, 0, OTHER)  # another body: still the stored one
    r.end("s1")
    r.process()
    r.ingest("s1", 0, 0, SPEECH)  # a retry after processing


def plain_late_chunk(r: Run) -> None:
    """A late chunk of a processed session: stored, another pass, same conversation."""
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    r.process()
    r.ingest("s1", 1, 30, OTHER)
    r.process()


def plain_idle(r: Run) -> None:
    r.ingest("s1", 0, 0, SPEECH)
    r.idle(100)
    r.idle(200)
    r.process()


def plain_silent_session(r: Run) -> None:
    r.ingest("s1", 0, 0, QUIET)
    r.end("s1")
    r.process()


def plain_transcription_fails(r: Run) -> None:
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    r.transcriber.fail = True
    r.process()
    r.transcriber.fail = False
    r.p.store.set_session_status("s1", "ended")
    r.process()


def plain_joins_no_meeting(r: Run) -> None:
    """An untracked session of another device next to a meeting never joins it."""
    r.ingest("m1", 0, 0, SPEECH, device="bot-test", track="a", name="Alice Example")
    r.end("m1")
    r.process()
    r.ingest("s1", 0, 60, OTHER, device="bot-test")
    r.end("s1")
    r.process()
    r.ingest("s2", 0, 120, SPEECH, device="bot-test")
    r.end("s2")
    r.process()


# --------------------------------------------------------------------- notes
def voice_note(r: Run) -> None:
    """A voice note: owner lines, no diarizer, no gap split, not joined with the plain
    conversation next to it."""
    r.ingest("s1", 0, 0, OTHER)
    r.end("s1")
    r.process()
    r.ingest("n1", 0, 40, OTHER, kind="note")
    for seq in range(1, 9):
        r.ingest("n1", seq, 40 + 30 * seq, QUIET, kind="note", arrive=40 + 30 * seq + 1)
    r.ingest("n1", 9, 310, SPEECH, kind="note")
    r.end("n1")
    r.process()
    r.ingest("n1", 10, 340, OTHER, kind="note")  # a late chunk extends the note
    r.process()
    r.ingest("n1", 11, 370, OTHER)  # another kind: refused


def voice_note_with_tracks(r: Run) -> None:
    """A note uploaded with a track: never stops on silence, transcribed as a note."""
    r.ingest("n1", 0, 0, SPEECH, kind="note", track="t1", name="Cleo Example")
    for seq in range(1, 4):
        r.ingest("n1", seq, 30 * seq, QUIET, kind="note", track="t1")
    r.end("n1")
    r.process()
    r.ingest("n1", 4, 120, SPEECH, kind="note", track="t1")


def written_note(r: Run) -> None:
    r.ingest("s1", 0, 0, SPEECH, device="handwriting2")
    r.end("s1")
    r.process()
    r.text("hub-note:1", "Buy milk\nCall Alice about the trip", 10, title="Errands")
    r.process()
    r.text("hub-note:1", "Buy milk\nCall Alice about the trip", 10, title="Errands")
    r.text("hub-note:1", "Buy oat milk\nCall Alice about the trip", 10)
    r.process()
    r.delete_text("hub-note:1")
    r.delete_text("hub-note:1")


# ------------------------------------------------------------------ meetings
ALICE, BO, CLEO = ("16778240", "Alice Example"), ("16779264", "Bo Example"), \
    ("16780288", "cleo example")


def meeting_tracks(r: Run) -> None:
    """Per-track sequences, a 10-minute break inside: one conversation, tracks named,
    never stopped on silence."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 6))), device="zoom", track=ALICE[0],
             name=ALICE[1])
    r.ingest("m1", 0, 0, chunk((10, tone(500, 6))), device="zoom", track=BO[0],
             name=BO[1])
    r.ingest("m1", 0, 0, chunk((20, tone(700, 4))), device="zoom", track=CLEO[0],
             name=CLEO[1])
    for seq in range(1, 4):  # quiet rounds: no silence stop for a meeting
        r.ingest("m1", seq, 30 * seq, QUIET, device="zoom", track=ALICE[0])
    r.ingest("m1", 4, 630, chunk((0, tone(300, 5))), device="zoom", track=ALICE[0])
    r.ingest("m1", 1, 640, chunk((0, tone(500, 5))), device="zoom", track=BO[0])
    r.ingest("m1", 1, 640, chunk((0, tone(500, 5))), device="zoom", track=BO[0])  # retry
    r.ingest("m1", 1, 640, chunk((0, tone(500, 5))), device="zoom", track=CLEO[0])
    r.end("m1")
    r.process()


def meeting_late_chunks(r: Run) -> None:
    """After a meeting is processed: a new chunk and a new track are refused, a retry
    of a stored chunk is a duplicate."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 6))), device="zoom", track=ALICE[0],
             name=ALICE[1])
    r.ingest("m1", 0, 0, chunk((5, tone(500, 6))), device="zoom", track=BO[0], name=BO[1])
    r.end("m1")
    r.process()
    r.ingest("m1", 1, 30, chunk((0, tone(300, 5))), device="zoom", track=BO[0])
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="zz")
    r.ingest("m1", 0, 0, chunk((0, tone(300, 6))), device="zoom", track=ALICE[0])
    r.ingest("m1", 5, 150, chunk((0, tone(300, 5))), device="zoom")  # untracked too
    r.process()


def meeting_max_duration_round(r: Run) -> None:
    """The round that reaches the max duration is stored for every track."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.ingest("m1", 1, 600, chunk((0, tone(300, 5))), device="zoom", track="a")
    r.ingest("m1", 0, 600, chunk((0, tone(500, 5))), device="zoom", track="b",
             name="Bo Example")
    r.process()
    r.ingest("m1", 1, 630, chunk((0, tone(500, 5))), device="zoom", track="b")


def meeting_back_to_back(r: Run) -> None:
    r.ingest("m1", 0, 0, chunk((20, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m1")
    r.process()
    r.ingest("m2", 0, 85, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m2")
    r.process()
    r.ingest("s1", 0, 150, SPEECH, device="zoom")  # an untracked Zoom session: its own
    r.end("s1")
    r.process()


def meeting_chat(r: Run) -> None:
    """Chat before processing waits in the session; chat after it is attached."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 6))), device="zoom", track=ALICE[0],
             name=ALICE[1])
    r.ingest("m1", 0, 0, chunk((10, tone(700, 4))), device="zoom", track=CLEO[0],
             name=CLEO[1])
    r.chat("m1", {"topic": "Budget review", "lines": [
        {"at": T0 + 12, "from": "Alice Example", "zoom_user_id": ALICE[0],
         "text": "Slides: https://example.com/q4.pdf"},
        {"at": T0 + 14, "from": "Bo Example", "zoom_user_id": BO[0], "text": "hi all"},
        {"at": T0 + 15, "from": "Bo Example", "text": "secret", "private": True}],
        "participants": [{"zoom_user_id": ALICE[0], "name": "Alice Example"},
                         {"zoom_user_id": BO[0], "name": "Bo Example"},
                         {"zoom_user_id": CLEO[0], "name": "Cleo Example"}]})
    r.end("m1")
    r.process()
    r.chat("m1", {"lines": [{"at": T0 + 25, "from": "Bo Example", "zoom_user_id": BO[0],
                             "text": "thanks, bye"}]})
    r.chat("m1", {"lines": [{"at": T0 + 25, "from": "Bo Example", "zoom_user_id": BO[0],
                             "text": "thanks, bye"}]})  # a retry: unchanged
    r.process()
    r.ingest("s9", 0, 0, SPEECH)
    r.chat("s9", {"topic": "not a meeting"})


def meeting_chat_only(r: Run) -> None:
    """The bot's chat-only meeting: one untracked silent second on device zoom, then chat;
    its conversation is a meeting of its own, not joined with a plain Zoom session."""
    r.ingest("z0", 0, -120, SPEECH, device="zoom")
    r.end("z0")
    r.process()
    r.ingest("z1", 0, 0, wav(silence(1)), device="zoom", arrive=1)
    r.chat("z1", {"topic": "Stand-up", "lines": [
        {"at": T0 + 2, "from": "Alice Example", "zoom_user_id": "1", "text": "running late"},
        {"at": T0 + 3, "from": "Cleo Example", "zoom_user_id": "2", "text": "ok"}]})
    r.end("z1")
    r.process()


def meeting_reopens_silence_stop(r: Run) -> None:
    """QA BUG-16: an untracked silent chunk stopped the session on silence and it was
    processed; the first tracked chunk reopens it; once a meeting, later chunks after
    processing are refused."""
    r.ingest("mix", 0, 0, QUIET, device="zoom", arrive=200)
    r.ingest("mix", 1, 30, QUIET, device="zoom", arrive=201)
    r.ingest("mix", 2, 60, QUIET, device="zoom", arrive=202)
    r.process()
    r.ingest("mix", 0, 90, chunk((0, tone(300, 5))), device="zoom", track="t1",
             name="Alice Example")
    r.ingest("mix", 1, 120, QUIET, device="zoom", track="t1")
    r.end("mix")
    r.process()
    r.ingest("mix", 2, 150, chunk((0, tone(300, 5))), device="zoom", track="t1")


def meeting_live(r: Run) -> None:
    """Live transcription while the meeting records, then the end pass."""
    for seq in range(0, 6):
        r.ingest("m1", seq, 30 * seq, chunk((2, tone(300, 6)), (14, tone(300, 6))),
                 device="zoom", track="a", name="Alice Example")
        r.live()
    r.end("m1")
    r.process()


def meeting_idle_with_active_bot(r: Run) -> None:
    """A meeting whose bot is still active isn't idle; a plain session is."""
    r.ingest("m1", 0, 0, SPEECH, device="zoom", track="a", name="Alice Example")
    r.ingest("s1", 0, 0, SPEECH)
    r.store._conn.execute(
        "INSERT INTO meetings (id, state, session_id, meeting_id, created_at)"
        " VALUES ('b1', 'recording', 'm1', '123', ?)", (T0,))
    r.store._conn.commit()
    r.idle(300)
    r.idle(900)
    r.process()


# ---------------------------------------------------------------- enrollment
def enrollment(r: Run) -> None:
    r.clock.now = T0 - 10
    voice_enroll.start(r.p, "Sam", is_owner=False, now=r.clock.now)
    r.ingest("e1", 0, 0, chunk((0, tone(900, 28))), device="phone")
    r.ingest("e1", 1, 30, chunk((0, tone(900, 28))), device="phone")
    r.end("e1")
    r.process()
    r.ingest("s1", 0, 400, chunk((0, tone(900, 6))))
    r.end("s1")
    r.process()


# --------------------------------------------------------------------- races
def race_late_meeting_chunk_before_worker(r: Run) -> None:
    """Ended, not yet processing: the chunk is stored and processed with the rest."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m1")
    r.ingest("m1", 1, 30, chunk((0, tone(300, 5))), device="zoom", track="a")
    r.process()


def race_meeting_closed_between_check_and_store(r: Run) -> None:
    """The early check passes (status ended), the worker starts during decode/VAD, then
    ``add_chunk`` refuses (``MeetingClosed``) and the file is removed."""
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m1")
    detect = r.p.vad.detect

    def worker_starts(pcm):
        r.store.set_session_status("m1", "processing")
        return detect(pcm)

    r.p.vad.detect = worker_starts
    r.ingest("m1", 1, 30, chunk((0, tone(300, 5))), device="zoom", track="a")
    r.p.vad.detect = detect
    r.store.set_session_status("m1", "ended")
    r.process()


def _during_transcription(r: Run, action: Callable[[], None]) -> None:
    """Run ``action`` on the fake whisper's first call (the worker is processing)."""
    original = r.transcriber.transcribe
    fired = []

    def transcribe(audio, prompt=""):
        if not fired:
            fired.append(1)
            action()
        return original(audio, prompt)

    r.transcriber.transcribe = transcribe


def race_meeting_chunk_during_processing(r: Run) -> None:
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m1")
    _during_transcription(r, lambda: r.ingest(
        "m1", 1, 30, chunk((0, tone(300, 5))), device="zoom", track="a"))
    r.process()
    r.process()


def race_plain_chunk_during_processing(r: Run) -> None:
    """A plain session takes it: not marked done, processed again on the next pass."""
    r.ingest("s1", 0, 0, SPEECH)
    r.end("s1")
    _during_transcription(r, lambda: r.ingest("s1", 1, 30, OTHER))
    r.process()
    r.process()


def race_tracked_chunk_during_processing_of_silence_stop(r: Run) -> None:
    """A plain zoom session stopped on silence is being processed when its first tracked
    chunk comes: the chunk reopens it, and this pass already files it as a meeting."""
    r.ingest("s1", 0, 0, chunk((0, tone(300, 5))), device="bot-test")
    r.ingest("s1", 1, 30, QUIET, device="bot-test")
    r.ingest("s1", 2, 60, QUIET, device="bot-test")
    r.ingest("p1", 0, -100, OTHER, device="bot-test")  # a plain conversation before it
    r.end("p1")
    r.process_session("p1")
    _during_transcription(r, lambda: r.ingest(
        "s1", 0, 90, chunk((0, tone(500, 5))), device="bot-test", track="t1",
        name="Bo Example"))
    r.process()
    r.end("s1")
    r.process()


def race_chat_during_processing(r: Run) -> None:
    r.ingest("m1", 0, 0, chunk((0, tone(300, 5))), device="zoom", track="a",
             name="Alice Example")
    r.end("m1")
    _during_transcription(r, lambda: r.chat("m1", {"topic": "Planning", "lines": [
        {"at": T0 + 3, "from": "Alice Example", "zoom_user_id": "a", "text": "agenda?"}]}))
    r.process()
    r.process()


SCENARIOS: dict[str, Callable[[Run], None]] = {f.__name__: f for f in [
    plain_gap_over, plain_gap_under, plain_next_session_within_gap,
    plain_next_session_beyond_gap, plain_other_device, plain_bridge_absorbs,
    plain_silence_stop, plain_max_duration, plain_retry, plain_late_chunk, plain_idle,
    plain_silent_session, plain_transcription_fails, plain_joins_no_meeting,
    voice_note, voice_note_with_tracks, written_note,
    meeting_tracks, meeting_late_chunks, meeting_max_duration_round, meeting_back_to_back,
    meeting_chat, meeting_chat_only, meeting_reopens_silence_stop, meeting_live,
    meeting_idle_with_active_bot,
    enrollment,
    race_late_meeting_chunk_before_worker, race_meeting_closed_between_check_and_store,
    race_meeting_chunk_during_processing, race_plain_chunk_during_processing,
    race_tracked_chunk_during_processing_of_silence_stop, race_chat_during_processing,
]}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_session_kind_table(name, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch)
    SCENARIOS[name](run)
    check(name, run.result())


def test_every_snapshot_has_a_scenario():
    if SNAPSHOTS.exists():
        assert {p.stem for p in SNAPSHOTS.glob("*.json")} <= set(SCENARIOS)
