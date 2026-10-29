"""Voiceprints: enrolled and learned voice samples, voice enrollment state."""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np

from .people import _ensure_person, _refresh_voiceprint, _unit, _voice_of

MAX_LEARNED_SAMPLES = 40  # per speaker, besides enrollment samples


class VoicesRepo:
    """``Store``'s methods for voiceprints. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    # ------------------------------------------------- voice enrollment (next recording)
    def get_voice_enrollment(self) -> dict[str, Any] | None:
        """The "enroll from the next recording" state (``voice_enroll.py``), or None."""
        row = self._one("SELECT state FROM voice_enrollment WHERE id = 1")
        return json.loads(row["state"]) if row else None

    def put_voice_enrollment(self, state: dict[str, Any] | None) -> None:
        """Replace that state; None removes it."""
        with self.tx() as c:
            if state is None:
                c.execute("DELETE FROM voice_enrollment WHERE id = 1")
            else:
                c.execute("INSERT INTO voice_enrollment (id, state, updated_at) VALUES (1, ?, ?)"
                          " ON CONFLICT(id) DO UPDATE SET state = excluded.state,"
                          " updated_at = excluded.updated_at",
                          (json.dumps(state), time.time()))

    # ---------------------------------------------------------------- speakers
    def add_voice_sample(self, name: str, embedding: np.ndarray, source: str = "named") -> None:
        """Teach the diarizer a voice: add a sample to a speaker (created if new)."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM speakers WHERE name = ?", (name,)).fetchone()
            if row is None:
                vec = _unit(embedding)
                sid = c.execute(
                    "INSERT INTO speakers (name, is_owner, embedding, created_at)"
                    " VALUES (?, 0, ?, ?)", (name, vec.tobytes(), time.time())).lastrowid
            else:
                sid = row["id"]
            _ensure_person(c, name, sid)
            c.execute("INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
                      " VALUES (?, ?, ?, ?)", (sid, _unit(embedding).tobytes(), source,
                                               time.time()))
            # keep every enrollment sample; drop the oldest learned ones beyond the cap
            c.execute(
                "DELETE FROM voice_samples WHERE id IN (SELECT id FROM voice_samples"
                " WHERE speaker_id = ? AND source != 'enrolled' ORDER BY created_at DESC,"
                " id DESC LIMIT -1 OFFSET ?)", (sid, MAX_LEARNED_SAMPLES))
            _refresh_voiceprint(c, sid)

    def put_meeting_voice_samples(self, name: str, session_id: str,
                                  samples: list[np.ndarray], cap: int) -> int:
        """Voice samples of ``name`` learned from a meeting (``source='meeting'``).

        The speaker is the person's linked voice (``people.speaker_id``, the person found
        by name or alias in any case), else a speaker of that name in any case, else new
        (never as the owner, and an existing speaker's ``is_owner`` is left alone): one
        voice per person, never a second row next to an enrolled one (QA BUG-06). Earlier
        samples of the same meeting are replaced; then only the newest ``cap`` meeting
        samples are kept. Enrolled and named samples are never touched. Returns how many
        meeting samples the speaker has now.
        """
        with self.tx() as c:
            row = _voice_of(c, name)
            if row is None:
                sid = c.execute(
                    "INSERT INTO speakers (name, is_owner, embedding, created_at)"
                    " VALUES (?, 0, ?, ?)",
                    (name, _unit(np.mean(samples, axis=0)).tobytes(), time.time())).lastrowid
            else:
                sid = row["id"]
            _ensure_person(c, name, sid)
            c.execute("DELETE FROM voice_samples WHERE speaker_id = ? AND source = 'meeting'"
                      " AND session_id = ?", (sid, session_id))
            now = time.time()
            c.executemany(
                "INSERT INTO voice_samples (speaker_id, embedding, source, created_at,"
                " session_id) VALUES (?, ?, 'meeting', ?, ?)",
                [(sid, _unit(v).tobytes(), now, session_id) for v in samples])
            c.execute(
                "DELETE FROM voice_samples WHERE id IN (SELECT id FROM voice_samples"
                " WHERE speaker_id = ? AND source = 'meeting' ORDER BY created_at DESC,"
                " id DESC LIMIT -1 OFFSET ?)", (sid, cap))
            _refresh_voiceprint(c, sid)
            return c.execute("SELECT COUNT(*) FROM voice_samples WHERE speaker_id = ?"
                             " AND source = 'meeting'", (sid,)).fetchone()[0]

    def voice_name(self, name: str) -> str | None:
        """The name of the voice (``speakers.name``) that samples for ``name`` go to (see
        ``put_meeting_voice_samples``), None when there is none yet."""
        with self.tx() as c:
            row = _voice_of(c, name)
            return row["name"] if row else None

    def voice_samples(self, name: str) -> list[dict[str, Any]]:
        """A speaker's voice samples, oldest first: source, session_id, created_at."""
        return self._q(
            "SELECT v.source, v.session_id, v.created_at FROM voice_samples v"
            " JOIN speakers s ON s.id = v.speaker_id WHERE s.name = ? ORDER BY v.id",
            (name,))

    def put_speaker(self, name: str, embedding: np.ndarray, is_owner: bool,
                    samples: list[np.ndarray] | None = None, source: str = "enrolled") -> None:
        """Create or update a speaker; ``samples`` replace their earlier ``source`` samples."""
        samples = samples if samples is not None else [embedding]
        with self.tx() as c:
            if is_owner:
                c.execute("UPDATE speakers SET is_owner = 0")
            c.execute(
                "INSERT INTO speakers (name, is_owner, embedding, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET is_owner = excluded.is_owner,"
                " embedding = excluded.embedding",
                (name, int(is_owner), _unit(embedding).tobytes(), time.time()))
            sid = c.execute("SELECT id FROM speakers WHERE name = ?", (name,)).fetchone()["id"]
            pid = _ensure_person(c, name, sid, is_owner)
            if pid is not None and not is_owner:
                c.execute("UPDATE people SET is_owner = 0 WHERE id = ? AND speaker_id = ?",
                          (pid, sid))
            c.execute("DELETE FROM voice_samples WHERE speaker_id = ? AND source = ?",
                      (sid, source))
            now = time.time()
            c.executemany(
                "INSERT INTO voice_samples (speaker_id, embedding, source, created_at)"
                " VALUES (?, ?, ?, ?)", [(sid, _unit(v).tobytes(), source, now) for v in samples])
            _refresh_voiceprint(c, sid)

    def speakers(self) -> list[dict[str, Any]]:
        """Enrolled/named voices: name, is_owner, embedding (mean), samples (n, dim)."""
        rows = self._q("SELECT id, name, is_owner, embedding FROM speakers"
                       " ORDER BY is_owner DESC, name")
        samples: dict[int, list[np.ndarray]] = {}
        for r in self._q("SELECT speaker_id, embedding FROM voice_samples ORDER BY id"):
            samples.setdefault(r["speaker_id"], []).append(
                np.frombuffer(r["embedding"], dtype=np.float32))
        for r in rows:
            r["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            r["is_owner"] = bool(r["is_owner"])
            # only samples of the voiceprint's speaker model (see _refresh_voiceprint)
            got = [v for v in samples.get(r.pop("id"), []) if v.size == r["embedding"].size]
            r["samples"] = (np.stack(got) if got
                            else np.zeros((0, r["embedding"].size), np.float32))
        return rows

    def owner_voice_name(self) -> str | None:
        """The enrolled owner voice's name (``speakers.is_owner``), None without one: the
        voice learned owner samples go to. The same as ``owner_name`` (which no longer
        falls back to the owner's person); kept as its own name so learning reads as
        "needs a voice"."""
        row = self._one("SELECT name FROM speakers WHERE is_owner = 1")
        return row["name"] if row else None
