"""Sessions, their chunks and transcript lines."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from ..domain import (
    _ACTIVE_IN,
    CHAT,
    CONVERSATION,
    MEETING_ACTIVE,
    MEETING_CLOSED_STATUSES,
    SPEECH,
    Segment,
)


class DuplicateChunk(Exception):
    pass


class MeetingClosed(Exception):
    """A new chunk for a meeting (a session with tracks) that is being or has been
    processed: storing it would make the next pass replace the other tracks' lines in
    its time range, whose audio is gone."""


def _segment(r: dict[str, Any]) -> Segment:
    return Segment(started_at=r["started_at"], ended_at=r["ended_at"], text=r["text"],
                   speaker=r["speaker"], is_owner=bool(r["is_owner"]), id=r["id"],
                   session_id=r["session_id"], device=r["device"],
                   conversation_id=r["conversation_id"], kind=r.get("kind") or SPEECH)


class SessionsRepo:
    """``Store``'s methods for sessions and their lines. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    def recordings_since(self, received_after: float, started_after: float,
                         skip_devices: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Sessions whose first chunk reached the server after ``received_after`` and
        that started after ``started_after``, oldest first, with ``seconds`` (audio
        received so far): recordings, not meetings (no tracks) and not ``skip_devices``."""
        skip = list(skip_devices)
        marks = ", ".join("?" * len(skip)) or "''"
        return self._q(
            "SELECT s.id, s.device, s.started_at, s.status, s.kind,"
            " SUM(c.duration) AS seconds FROM sessions s JOIN chunks c ON c.session_id = s.id"
            f" WHERE s.started_at >= ? AND s.device NOT IN ({marks})"
            " GROUP BY s.id HAVING MIN(c.received_at) >= ? AND MAX(c.track) = ''"
            " ORDER BY s.started_at, s.id", [started_after, *skip, received_after])

    # ---------------------------------------------------------------- sessions
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def add_chunk(
        self,
        *,
        session_id: str,
        device: str,
        sequence: int,
        started_at: float,
        duration: float,
        codec: str,
        audio_path: str | None,
        speech: list[tuple[float, float]],
        now: float | None = None,
        kind: str = CONVERSATION,
        track: str = "",
        track_name: str | None = None,
    ) -> dict[str, Any]:
        """Record a chunk and update the session's silence bookkeeping.

        ``track`` ('' = none) keeps one participant's chunks apart; sequences count per
        track. ``track_name`` is the track's display name when this chunk was recorded.

        Returns the updated session row. Raises DuplicateChunk on a repeated
        (session, track, sequence) so device retries are idempotent (the retry's
        ``track_name`` is ignored too), and ValueError if the session belongs to another
        device or is of another ``kind``. The session's silence bookkeeping spans all
        tracks: ``last_speech_at`` is the latest speech on any track. Raises
        MeetingClosed for a new chunk of a meeting (a session with tracked chunks) that is
        being or was processed; a session without tracks takes it and goes back for
        another pass. A tracked chunk in a session an untracked chunk stopped on silence
        reopens it ('recording', no stop reason), since a tracked session never stops on
        silence; other stops stand.
        """
        now = time.time() if now is None else now
        end = started_at + duration
        last_speech = started_at + max(e for _, e in speech) if speech else None
        with self.tx() as c:
            sess = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if sess is None:
                c.execute(
                    "INSERT INTO sessions (id, device, started_at, last_chunk_at, last_speech_at,"
                    " received_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, device, started_at, end, last_speech, now, kind),
                )
            elif sess["device"] != device:
                raise ValueError("session_id already belongs to another device")
            elif sess["kind"] != kind:
                raise ValueError(f"session {session_id} is a {sess['kind']} session, "
                                 f"not {kind}")
            if (sess is not None and sess["status"] in MEETING_CLOSED_STATUSES
                    and ((track and sess["stop_reason"] != "silence")
                         or c.execute("SELECT 1 FROM chunks WHERE session_id = ? AND"
                                      " track != '' LIMIT 1", (session_id,)).fetchone())
                    and not c.execute("SELECT 1 FROM chunks WHERE session_id = ? AND"
                                      " track = ? AND sequence = ?",
                                      (session_id, track, sequence)).fetchone()):
                raise MeetingClosed(session_id)
            try:
                c.execute(
                    "INSERT INTO chunks (session_id, sequence, started_at, duration, codec,"
                    " audio_path, speech, received_at, track, track_name)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, sequence, started_at, duration, codec, audio_path,
                     json.dumps(speech), now, track, track_name),
                )
            except sqlite3.IntegrityError as e:
                where = f"{track}#" if track else ""
                raise DuplicateChunk(f"{session_id}#{where}{sequence}") from e
            if sess is not None:
                # Chunks may arrive out of order (offline queues); keep maxima.
                c.execute(
                    "UPDATE sessions SET started_at = MIN(started_at, ?),"
                    " last_chunk_at = MAX(last_chunk_at, ?),"
                    " last_speech_at = CASE WHEN ? IS NULL THEN last_speech_at"
                    "   ELSE MAX(COALESCE(last_speech_at, 0), ?) END,"
                    " received_at = ?,"
                    # A tracked session never stops on silence: a tracked chunk undoes a
                    # silence stop an untracked chunk made, and the session records again.
                    " status = CASE WHEN ? != '' AND stop_reason = 'silence' THEN 'recording'"
                    # A chunk for a session being processed or already processed sends
                    # it back for another pass (store-and-forward relays upload late).
                    " WHEN status IN ('processing','done','failed') THEN 'ended'"
                    " ELSE status END,"
                    " stop_reason = CASE WHEN ? != '' AND stop_reason = 'silence' THEN NULL"
                    " ELSE stop_reason END"
                    " WHERE id = ?",
                    (started_at, end, last_speech, last_speech, now, track, track,
                     session_id),
                )
            return dict(c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def end_session(self, session_id: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET status = 'ended', stop_reason = COALESCE(stop_reason, ?)"
                " WHERE id = ? AND status = 'recording'",
                (reason, session_id),
            )

    def set_session_status(self, session_id: str, status: str, error: str | None = None,
                           *, only_if: str | None = None) -> bool:
        """Set the status; with ``only_if``, only while the session still has that status."""
        sql, params = "UPDATE sessions SET status = ?, error = ? WHERE id = ?", [status, error,
                                                                                session_id]
        if only_if is not None:
            sql += " AND status = ?"
            params.append(only_if)
        with self.tx() as c:
            return c.execute(sql, params).rowcount > 0

    def idle_sessions(self, received_before: float,
                      bot_started_after: float | None = None) -> list[str]:
        """Recording sessions with no upload since ``received_before``. With
        ``bot_started_after``, a session whose meeting bot is still active and was started
        after that time is left out: Zoom sends audio only while someone talks, so a
        muted stretch uploads nothing while the bot still records; its exit ends the
        session (meetings/bot/launcher.py)."""
        sql, params = ("SELECT id FROM sessions WHERE status = 'recording' AND received_at < ?",
                       [received_before])
        if bot_started_after is not None:
            sql += (" AND id NOT IN (SELECT session_id FROM meetings WHERE"
                    f" state IN ({_ACTIVE_IN}) AND created_at > ?)")
            params += [*MEETING_ACTIVE, bot_started_after]
        return [r["id"] for r in self._q(sql, params)]

    def requeue_interrupted(self) -> int:
        """Send sessions left in 'processing' (server stopped mid-run) back to 'ended'."""
        with self.tx() as c:
            return c.execute(
                "UPDATE sessions SET status = 'ended' WHERE status = 'processing'").rowcount

    def sessions_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM sessions WHERE status = ? ORDER BY started_at", (status,))]

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._q(
            "SELECT s.*, (SELECT COUNT(*) FROM chunks c WHERE c.session_id = s.id) AS chunk_count"
            " FROM sessions s ORDER BY started_at DESC LIMIT ?", (limit,))

    def chunks(self, session_id: str, track: str | None = None) -> list[dict[str, Any]]:
        """The session's chunk rows ordered by (track, sequence); only ``track``'s when
        given ('' = the chunks without a track). Each row has ``track`` ('' = none),
        ``track_name`` (or None) and ``speech`` as a list of (start, end) tuples."""
        sql, params = "SELECT * FROM chunks WHERE session_id = ?", [session_id]
        if track is not None:
            sql += " AND track = ?"
            params.append(track)
        rows = self._q(sql + " ORDER BY track, sequence", params)
        for r in rows:
            r["speech"] = [tuple(s) for s in json.loads(r["speech"])]
        return rows

    def has_tracks(self, session_id: str) -> bool:
        """Whether the session has any chunk with a track (a meeting)."""
        return self._one("SELECT 1 AS x FROM chunks WHERE session_id = ? AND track != ''"
                         " LIMIT 1", (session_id,)) is not None

    def has_chunk(self, session_id: str, track: str, sequence: int) -> bool:
        """Whether (session, track, sequence) is already stored ('' = no track)."""
        return self._one("SELECT 1 AS x FROM chunks WHERE session_id = ? AND track = ?"
                         " AND sequence = ?", (session_id, track, sequence)) is not None

    def clear_chunk_audio(self, session_id: str,
                          keys: Iterable[tuple[str, int]] | None = None) -> None:
        """Forget the audio of the given chunks, as (track, sequence) pairs ('' = no
        track); all chunks of the session if None."""
        with self.tx() as c:
            if keys is None:
                c.execute("UPDATE chunks SET audio_path = NULL WHERE session_id = ?",
                          (session_id,))
            else:
                c.executemany(
                    "UPDATE chunks SET audio_path = NULL WHERE session_id = ? AND track = ?"
                    " AND sequence = ?",
                    [(session_id, track, seq) for track, seq in keys])

    # ---------------------------------------------------------------- segments
    def replace_segments(self, session_id: str, device: str, segments: list[Segment],
                         ranges: list[tuple[float, float]]) -> None:
        """Replace the session's segments that start inside the re-transcribed ``ranges``.

        Segments outside those ranges (from chunks whose audio is already deleted)
        are kept, so late chunks for a finished session don't wipe its transcript. Chat
        lines (kind CHAT) never come from audio, so a pass never replaces them.
        """
        with self.tx() as c:
            c.executemany(
                "DELETE FROM segments WHERE session_id = ? AND started_at >= ? AND started_at < ?"
                " AND kind != ?",
                [(session_id, a, b, CHAT) for a, b in ranges])
            c.executemany(
                "INSERT INTO segments (session_id, device, started_at, ended_at, speaker,"
                " is_owner, text, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(session_id, device, s.started_at, s.ended_at, s.speaker, int(s.is_owner),
                  s.text, s.kind) for s in segments],
            )

    def session_segments(self, session_id: str) -> list[Segment]:
        rows = self._q("SELECT * FROM segments WHERE session_id = ? ORDER BY started_at",
                       (session_id,))
        return [_segment(r) for r in rows]

    def conversation_segments(self, conversation_id: str) -> list[Segment]:
        rows = self._q("SELECT * FROM segments WHERE conversation_id = ? ORDER BY started_at",
                       (conversation_id,))
        return [_segment(r) for r in rows]
