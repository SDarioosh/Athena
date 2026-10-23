"""Device-agnostic processing pipeline.

ingest_chunk  -> decode, VAD, persist, silence/max-duration stop decision
process_session -> reassemble by sequence, transcribe speech regions (whisper.cpp),
                   diarize, store segments, delete raw audio, split into conversations,
                   summarize (llama-server), embed
make_digest   -> daily digest over the day's summarized conversations
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from . import audio as audio_mod
from .clips import plan_windows, tail_words
from .config import Settings
from .conversations import split_on_gaps
from .diarize import Diarizer, PieceRef, load_voices
from .llm import Summarizer
from .search import Embedder, index_conversation
from .store import CONVERSATION, NOTE, SESSION_KINDS, Segment, Store
from .transcribe import Transcriber
from .vad import VAD, Span, merge_spans

log = logging.getLogger(__name__)

_SAFE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CONTIGUOUS_TOLERANCE = 1.5  # seconds between chunk end and next chunk start
REGION_MERGE_GAP = 2.0
# ASR gain (CONVO_ASR_AGC_MAX_GAIN) only touches clips whose peak is at or below this
# fraction of full scale: Omi measured more errors when already-loud audio was gained.
ASR_AGC_DEADBAND = 0.4
# whisper reads at most ~224 prompt tokens; names beyond this many are left out.
MAX_PROMPT_NAMES = 30
_PLACEHOLDER_NAME = re.compile(r"^(speaker\s*\d+|unknown|me)$", re.IGNORECASE)


def validate_id(value: str, what: str) -> str:
    if not _SAFE.match(value):
        raise ValueError(f"{what} must match {_SAFE.pattern}")
    return value


@dataclass
class IngestResult:
    stop: bool
    reason: str | None
    speech_seconds: float
    trailing_silence_seconds: float
    session_seconds: float
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "stop": self.stop, "reason": self.reason,
                "duplicate": self.duplicate,
                "speech_seconds": round(self.speech_seconds, 2),
                "trailing_silence_seconds": round(self.trailing_silence_seconds, 2),
                "session_seconds": round(self.session_seconds, 2)}


class Pipeline:
    def __init__(self, settings: Settings, store: Store, vad: VAD,
                 transcriber: Transcriber | None = None, diarizer: Diarizer | None = None,
                 summarizer: Summarizer | None = None,
                 embedder: Embedder | None = None) -> None:
        self.settings = settings
        self.store = store
        self.vad = vad
        self.transcriber = transcriber
        self.diarizer = diarizer
        self.summarizer = summarizer
        self.embedder = embedder
        self._process_lock = threading.Lock()

    # ------------------------------------------------------------------ ingest
    def ingest_chunk(self, *, device: str, session_id: str, sequence: int, started_at: float,
                     codec: str, data: bytes, sample_rate: int = audio_mod.SAMPLE_RATE,
                     now: float | None = None, kind: str = CONVERSATION) -> IngestResult:
        from .store import DuplicateChunk

        validate_id(device, "device")
        validate_id(session_id, "session_id")
        if sequence < 0:
            raise ValueError("sequence must be >= 0")
        kind = (kind or CONVERSATION).strip().lower()
        if kind not in SESSION_KINDS:
            raise ValueError(f"kind must be one of {list(SESSION_KINDS)}")
        codec = codec.lower()
        if codec not in audio_mod.KNOWN_CODECS:
            raise ValueError(f"codec must be one of {sorted(audio_mod.KNOWN_CODECS)}")
        if not data:
            raise ValueError("empty audio")
        pcm = audio_mod.decode(data, codec, sample_rate)
        if codec == "pcm16" and sample_rate != audio_mod.SAMPLE_RATE:
            # Raw PCM carries no rate, and processing decodes the stored file as 16 kHz:
            # store it at 16 kHz.
            data = audio_mod.float_to_pcm16(pcm)
        duration = audio_mod.duration_seconds(pcm)
        spans = self.vad.detect(pcm)
        speech = [(round(s.start, 3), round(s.end, 3)) for s in spans]

        path = part = None
        if data:
            d = self.settings.audio_dir / device / session_id
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{sequence:06d}.{codec}"
            # Unique temp name: a retry racing the original upload of the same chunk
            # must not overwrite or delete the original's temp file.
            part = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
            part.write_bytes(data)
        try:
            sess = self.store.add_chunk(session_id=session_id, device=device, sequence=sequence,
                                        started_at=started_at, duration=duration, codec=codec,
                                        audio_path=str(path) if path else None, speech=speech,
                                        now=now, kind=kind)
            duplicate = False
            if part and path:
                part.replace(path)
        except DuplicateChunk:
            # Retry of an already-stored chunk: keep the original, answer consistently.
            sess = self.store.get_session(session_id)
            assert sess is not None
            duplicate = True
        finally:
            if part:
                part.unlink(missing_ok=True)

        speech_end = sess["last_speech_at"] or sess["started_at"]
        trailing = max(0.0, sess["last_chunk_at"] - speech_end)
        length = sess["last_chunk_at"] - sess["started_at"]
        reason = None
        if sess["status"] != "recording":
            reason = sess["stop_reason"] or "ended"
        elif trailing >= self.settings.silence_stop_seconds:
            reason = "silence"
        elif length >= self.settings.max_session_seconds:
            reason = "max_duration"
        if reason and sess["status"] == "recording":
            self.store.end_session(session_id, reason)
        return IngestResult(stop=reason is not None, reason=reason,
                            speech_seconds=sum(e - s for s, e in speech),
                            trailing_silence_seconds=trailing, session_seconds=length,
                            duplicate=duplicate)

    def end_session(self, session_id: str, reason: str = "client") -> bool:
        if self.store.get_session(session_id) is None:
            return False
        self.store.end_session(session_id, reason)
        return True

    def finalize_idle(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        ids = self.store.idle_sessions(now - self.settings.session_idle_seconds)
        for sid in ids:
            self.store.end_session(sid, "idle")
        return ids

    # ----------------------------------------------------------------- process
    def process_pending(self, now: float | None = None) -> int:
        """Process every ended session, then summarize what is still unsummarized.

        This is the worker's pass, so failed summaries are retried with the worker's
        back-off (``CONVO_SUMMARY_RETRY_SECONDS``), not on every pass.
        """
        n = 0
        for sid in self.store.sessions_with_status("ended"):
            self.process_session(sid)
            n += 1
        self.summarize_pending(retry_after=self.settings.summary_retry_seconds,
                               stop_on_failure=True, now=now)
        return n

    def process_session(self, session_id: str) -> None:
        with self._process_lock:
            sess = self.store.get_session(session_id)
            if sess is None:
                raise KeyError(session_id)
            self.store.set_session_status(session_id, "processing")
            note = sess.get("kind") == NOTE
            try:
                segments, ranges, done = self._transcribe_session(session_id, note=note)
            except Exception as e:  # keep audio so the session can be retried
                log.exception("transcription failed for %s", session_id)
                self.store.set_session_status(session_id, "failed", f"{type(e).__name__}: {e}",
                                              only_if="processing")
                return
            self.store.replace_segments(session_id, sess["device"], segments, ranges)
            if not self.settings.keep_audio:
                # Only the chunks transcribed here: one uploaded meanwhile waits for the
                # next pass (add_chunk set the session back to 'ended').
                self._delete_audio(session_id, sess["device"], done)
            self.store.delete_empty_conversations()
            if note:
                touched = self._assign_note(session_id, sess["device"])
            else:
                touched = self._assign_conversations(session_id, sess["device"])
            self.store.set_session_status(session_id, "done", only_if="processing")
        for conv_id in touched:
            self.summarize_conversation(conv_id)

    def _transcribe_session(
        self, session_id: str, *, note: bool = False
    ) -> tuple[list[Segment], list[tuple[float, float]], list[dict[str, Any]]]:
        """Transcribe chunks that still have audio.

        Returns segments, the covered time ranges and the chunks that were transcribed.
        A voice note (``note``) skips the diarizer: every line is the owner's.
        """
        chunks = [c for c in self.store.chunks(session_id) if c["audio_path"]]
        ranges = [(c["started_at"], c["started_at"] + c["duration"]) for c in chunks]
        if not any(c["speech"] for c in chunks):
            return [], ranges, chunks
        if self.transcriber is None:
            raise RuntimeError("no transcriber configured (set CONVO_WHISPER_SERVER_URL)")
        prompt = self.whisper_prompt()
        runs: list[np.ndarray] = []
        pieces: list[tuple[float, float, str, PieceRef]] = []
        piece_words: list[list[tuple[float, float, str]]] = []  # run time, per piece
        cfg = self.settings
        context = ""
        for run_start, run_audio, run_speech in self._runs(chunks):
            runs.append(run_audio)
            regions = merge_spans(run_speech, REGION_MERGE_GAP)
            windows = plan_windows(regions, run_audio.size / audio_mod.SAMPLE_RATE,
                                   pad=cfg.whisper_pad_seconds,
                                   max_seconds=cfg.whisper_window_seconds,
                                   max_gap=cfg.whisper_window_gap_seconds)
            for w in windows:
                # The diarizer keeps the ungained run audio; only the transcriber hears
                # the gain (CONVO_ASR_AGC_MAX_GAIN).
                clip, _ = audio_mod.bounded_gain(w.extract(run_audio),
                                                 max_gain=cfg.asr_agc_max_gain,
                                                 deadband=ASR_AGC_DEADBAND)
                got = self.transcriber.transcribe(
                    clip, " ".join(x for x in (prompt, context) if x))
                for p in (q for piece in got for q in w.split(piece)):
                    s = w.to_run(p.start)
                    e = max(s, w.to_run(max(p.end, p.start), end=True))
                    s, e = w.clamp(s, e)
                    pieces.append((run_start + s, run_start + e, p.text,
                                   PieceRef(len(runs) - 1, s, e)))
                    piece_words.append([(w.to_run(a), w.to_run(b, end=True), t)
                                        for a, b, t in p.words])
                if cfg.whisper_context_words > 0 and got:
                    context = tail_words(" ".join([context] + [p.text for p in got]),
                                         cfg.whisper_context_words)
        order = sorted(range(len(pieces)), key=lambda i: pieces[i][0])
        pieces = [pieces[i] for i in order]
        piece_words = [piece_words[i] for i in order]
        labels = None
        owner_name = (self.store.owner_name() or self.settings.owner_name) if note else ""
        if not note and self.diarizer is not None and pieces:
            self.diarizer.voices = load_voices(self.store)  # pick up newly named speakers
            labels, unnamed = self.diarizer.diarize_runs(runs, [p[3] for p in pieces])
            self.store.put_speaker_candidates(session_id, unnamed)
            if cfg.speaker_split_lines:
                pieces, labels = self._split_pieces(runs, pieces, piece_words, labels)
            if cfg.speaker_line_check:  # after splitting: each part is checked on its own
                labels = self.diarizer.check_lines(runs, [p[3] for p in pieces], labels,
                                                   cfg.speaker_line_floor)
        segments = []
        for i, (s, e, text, _) in enumerate(pieces):
            if note:
                name, owner = owner_name, True
            else:
                name, owner = ((labels[i].name, labels[i].is_owner) if labels
                               else ("Unknown", False))
            segments.append(Segment(started_at=s, ended_at=e, text=text, speaker=name,
                                    is_owner=owner))
        return segments, ranges, chunks

    def _split_pieces(self, runs: list[np.ndarray],
                      pieces: list[tuple[float, float, str, PieceRef]],
                      piece_words: list[list[tuple[float, float, str]]],
                      labels: list[Any]) -> tuple[list[tuple[float, float, str, PieceRef]],
                                                  list[Any]]:
        """CONVO_SPEAKER_SPLIT_LINES: a line over a speaker change becomes one line per
        speaker (``Diarizer.split_lines``), cut at word times."""
        parts = self.diarizer.split_lines(runs, [p[3] for p in pieces], piece_words, labels)
        out, out_labels = [], []
        for (s, e, text, ref), ws, lab, cut in zip(pieces, piece_words, labels, parts,
                                                     strict=True):
            if len(cut) <= 1:
                out.append((s, e, text, ref))
                out_labels.append(cut[0][2] if cut else lab)
                continue
            base = s - ref.start  # wall-clock time of the run's start
            for a, b, who in cut:
                ra, rb = ws[a][0], ws[b - 1][1]
                out.append((base + ra, base + rb, "".join(w[2] for w in ws[a:b]).strip(),
                            PieceRef(ref.run, ra, rb)))
                out_labels.append(who)
        return out, out_labels

    def whisper_prompt(self) -> str:
        """CONVO_WHISPER_PROMPT plus the names of known speakers, for whisper's spelling."""
        known = ([s["name"] for s in self.store.speakers()]
                 + [s["name"] for s in self.store.speaker_stats()]
                 if self.settings.whisper_prompt_names else [])
        names: list[str] = []
        for n in known:
            if not _PLACEHOLDER_NAME.match(n) and n.lower() not in {x.lower() for x in names}:
                names.append(n)
        parts = [self.settings.whisper_prompt.strip()]
        if names[:MAX_PROMPT_NAMES]:
            parts.append(", ".join(names[:MAX_PROMPT_NAMES]) + ".")
        return " ".join(p for p in parts if p)

    def _runs(self, chunks: list[dict[str, Any]]):
        """Yield (epoch_start, audio, speech_spans) for runs of contiguous chunks."""
        run: list[dict[str, Any]] = []

        def flush():
            if not run:
                return None
            start = run[0]["started_at"]
            parts, spans, offset = [], [], 0.0
            for c in run:
                pcm = self._load_chunk(c)
                parts.append(pcm)
                spans += [Span(offset + s, offset + e) for s, e in c["speech"]]
                offset += pcm.size / audio_mod.SAMPLE_RATE
            return start, np.concatenate(parts) if parts else np.zeros(0, np.float32), spans

        for c in chunks:
            if c["audio_path"] is None:
                continue
            if run:
                prev = run[-1]
                gap = c["started_at"] - (prev["started_at"] + prev["duration"])
                if abs(gap) > CONTIGUOUS_TOLERANCE or c["sequence"] != prev["sequence"] + 1:
                    out = flush()
                    if out:
                        yield out
                    run = []
            run.append(c)
        out = flush()
        if out:
            yield out

    def _load_chunk(self, chunk: dict[str, Any]) -> np.ndarray:
        data = Path(chunk["audio_path"]).read_bytes()
        return audio_mod.decode(data, chunk["codec"])

    def _delete_audio(self, session_id: str, device: str,
                      chunks: list[dict[str, Any]]) -> None:
        for c in chunks:
            if c["audio_path"]:
                Path(c["audio_path"]).unlink(missing_ok=True)
        self.store.clear_chunk_audio(session_id, [c["sequence"] for c in chunks])
        # rmdir, not rmtree: an upload in flight has its .part file in this folder.
        with contextlib.suppress(OSError):
            (self.settings.audio_dir / device / session_id).rmdir()

    def _assign_conversations(self, session_id: str, device: str) -> list[str]:
        gap = self.settings.conversation_gap_seconds
        touched: list[str] = []
        for group in split_on_gaps(self.store.session_segments(session_id), gap):
            start, end = group[0].started_at, max(s.ended_at for s in group)
            near = [c["id"] for c in self.store.overlapping_conversations(device, start, end, gap)]
            conv_id = self.store.save_conversation(near[0] if near else None, device,
                                                   [s.id for s in group if s.id is not None],
                                                   absorb=near[1:])
            touched = [t for t in touched if t not in near] + [conv_id]
        return touched

    def _assign_note(self, session_id: str, device: str) -> list[str]:
        """A voice note is one conversation of kind NOTE: no gap split, never joined with
        nearby conversations. A late chunk extends the note's existing conversation."""
        segments = self.store.session_segments(session_id)
        if not segments:
            return []
        existing = self.store.session_conversations(session_id)
        conv_id = self.store.save_conversation(
            existing[0] if existing else None, device,
            [s.id for s in segments if s.id is not None], absorb=existing[1:], kind=NOTE)
        return [conv_id]

    # --------------------------------------------------------------- summaries
    def summarize_conversation(self, conv_id: str) -> bool:
        if self.summarizer is None:
            return False
        segments = self.store.conversation_segments(conv_id)
        if not segments:
            return False
        conv = self.store.get_conversation(conv_id)
        kind = conv.get("kind") if conv else None
        try:
            summary = self.summarizer.summarize(segments, kind=kind)
        except Exception as e:
            log.exception("summary failed for %s", conv_id)
            self.store.mark_conversation_failed(conv_id, f"{type(e).__name__}: {e}")
            return False
        if summary.get("discard"):
            log.info("conversation %s discarded: %s", conv_id, summary["discard_reason"])
            self.store.save_discarded(conv_id, summary["discard_reason"], summary.get("kind"))
            return True
        fact_ids = self.store.save_summary(conv_id, summary)
        try:
            self._link_facts(conv_id, fact_ids, segments[0].started_at)
        except Exception:  # the summary is saved; facts just stay unlinked
            log.exception("linking facts failed for %s", conv_id)
        if self.embedder is not None:
            try:
                index_conversation(self.store, self.embedder, conv_id)
            except Exception:
                log.exception("embedding failed for %s (FTS search still works)", conv_id)
        return True

    def _link_facts(self, conv_id: str, fact_ids: list[int], started_at: float) -> None:
        """Merge new facts with known ones: drop repeats, let newer facts replace older."""
        assert self.summarizer is not None
        new = self.store.facts_by_id(fact_ids)
        known = self.store.related_facts(conv_id, new)
        if not new or not known:
            return
        tz = ZoneInfo(self.settings.timezone)
        for k in known:
            k["learned"] = datetime.fromtimestamp(k["conversation_at"], tz).strftime("%Y-%m-%d")
        date = datetime.fromtimestamp(started_at, tz).strftime("%Y-%m-%d")
        for link in self.summarizer.link_facts(date, new, known):
            if link.relation != "new" and link.existing is not None:
                self.store.link_fact(new[link.new]["id"], link.relation, link.existing)

    def summarize_pending(self, *, retry_after: float = 0.0, stop_on_failure: bool = False,
                          now: float | None = None) -> int:
        """Summarize pending conversations and retry failed ones; return how many succeeded.

        A failed conversation is retried once its last failure is at least
        ``retry_after`` seconds old, oldest failure first. With ``stop_on_failure`` the
        retries stop at the first one that fails again: llama-server is most likely
        still down, so the rest wait for a later pass instead of each trying it now.
        A failure refreshes the conversation's ``updated_at``, so it moves to the back
        of the queue and a broken conversation can't block the others.
        """
        # Snapshot both lists first, so a pending conversation that fails below isn't
        # retried again in the same call.
        now = time.time() if now is None else now
        pending = self.store.conversations_with_status("pending")
        failed = self.store.failed_conversations(before=now - retry_after)
        done = sum(self.summarize_conversation(c) for c in pending)
        for conv_id in failed:
            if self.summarize_conversation(conv_id):
                done += 1
            elif stop_on_failure:
                break
        return done

    # ------------------------------------------------------------------ digest
    def day_bounds(self, date: str) -> tuple[float, float]:
        tz = ZoneInfo(self.settings.timezone)
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
        return day.timestamp(), (day + timedelta(days=1)).timestamp()

    def digest_conversations(self, date: str) -> list[dict[str, Any]]:
        """The summarized conversations that started on ``date`` (local day), in order."""
        start, end = self.day_bounds(date)
        convs = [self.store.get_conversation(c["id"]) for c in
                 self.store.list_conversations(since=start, until=end, limit=500)]
        return sorted((c for c in convs if c and c["status"] == "summarized"
                       and start <= c["started_at"] < end), key=lambda c: c["started_at"])

    def make_digest(self, date: str) -> dict[str, Any] | None:
        if self.summarizer is None:
            raise RuntimeError("no LLM configured")
        convs = self.digest_conversations(date)
        if not convs:
            return None
        if self.store.rewrite_pending(c["id"] for c in convs):
            return None  # a forget took a summary down: wait for its rewrite
        digest = self.summarizer.digest(date, convs)
        self.store.put_digest(date, digest)
        return digest


    # ---------------------------------------------------------------- retention
    def prune_transcripts(self, now: float | None = None) -> dict[str, Any] | None:
        """Apply ``CONVO_TRANSCRIPT_RETENTION_DAYS`` (0 = keep forever: nothing runs)."""
        days = self.settings.transcript_retention_days
        if days <= 0:
            return None
        now = time.time() if now is None else now
        out = self.store.prune_transcripts(now - days * 86400)
        for path in out["audio"]:
            Path(path).unlink(missing_ok=True)
        if out["conversations"] or out["audio"]:
            log.info("retention: removed the transcripts of %d conversation(s) (%d lines) "
                     "and %d audio chunk(s) older than %d days", out["conversations"],
                     out["lines"], len(out["audio"]), days)
        return out

    # --------------------------------------------------------------- forgetting
    def rewrite_forgotten(self) -> int:
        """Rewrite the summaries ``erase_fact`` took down, then write again the digests it
        took down once none of their conversations waits. Stops at the first model
        failure (llama-server is most likely down; the rest wait for a later pass).
        Returns how many conversations were rewritten."""
        if self.summarizer is None:
            return 0
        done = 0
        for row in self.store.pending_rewrites():
            conv = self.store.get_conversation(row["conversation_id"])
            if conv is None or conv["status"] != "summarized":
                self.store.drop_rewrite(row["conversation_id"])
                continue
            try:
                fields = self.summarizer.remove_facts(row, row["facts"])
            except Exception:
                log.exception("forget rewrite failed for %s", row["conversation_id"])
                self.store.rewrite_failed(row["conversation_id"])
                break
            if not self.store.finish_rewrite(row["conversation_id"], row["queued_at"], fields):
                continue  # erased again meanwhile: the next pass rewrites the new state
            done += 1
            if self.embedder is not None:
                try:
                    index_conversation(self.store, self.embedder, row["conversation_id"])
                except Exception:
                    log.exception("embedding failed for %s", row["conversation_id"])
        for d in self.store.pending_digests():
            if self.store.rewrite_pending(d["conversation_ids"]):
                continue
            try:
                self.make_digest(d["date"])
            except Exception:
                log.exception("digest after a forget failed for %s", d["date"])
                break
            self.store.drop_pending_digest(d["date"])
        if done:
            self.store.compact()
        return done


class Worker(threading.Thread):
    """Background loop: finalize idle sessions, process, and write yesterday's digest."""

    def __init__(self, pipeline: Pipeline, interval: float = 15.0) -> None:
        super().__init__(name="convo-worker", daemon=True)
        self.pipeline = pipeline
        self.interval = interval
        self._stop_evt = threading.Event()
        self._last_prune = 0.0

    def start(self) -> None:
        # A restart (e.g. a deploy) during processing leaves sessions in 'processing',
        # which nothing picks up again. Only one worker runs per database.
        n = self.pipeline.store.requeue_interrupted()
        if n:
            log.info("requeued %d session(s) interrupted by a restart", n)
        super().start()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:  # pragma: no cover - timing loop
        while not self._stop_evt.wait(self.interval):
            self.tick()

    def tick(self, now: float | None = None) -> None:
        """One worker pass (``run`` calls this every ``interval`` seconds)."""
        try:
            self.pipeline.finalize_idle(now)
            self.pipeline.process_pending(now)
            self.pipeline.rewrite_forgotten()
            self._maybe_digest()
            self._maybe_prune()
        except Exception:
            log.exception("worker iteration failed")

    def _maybe_prune(self) -> None:
        """Retention, at most once an hour (it only matters by the day)."""
        if self.pipeline.settings.transcript_retention_days <= 0:
            return
        now = time.time()
        if now - self._last_prune >= 3600:
            self._last_prune = now
            self.pipeline.prune_transcripts(now)

    def _maybe_digest(self) -> None:
        """Write yesterday's digest, and write it again when the conversations of that day
        changed (a relay uploaded a recording late, conversations were merged)."""
        p = self.pipeline
        if p.summarizer is None or p.store.sessions_with_status("ended"):
            return
        tz = ZoneInfo(p.settings.timezone)
        yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
        digest = p.store.get_digest(yesterday)
        if digest is not None and set(digest.get("conversation_ids", [])) == {
                c["id"] for c in p.digest_conversations(yesterday)}:
            return
        p.make_digest(yesterday)
