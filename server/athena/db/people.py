"""People and speaker names: stable ids, aliases, renames and merges."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from typing import Any

import numpy as np

from ..domain import _PLACEHOLDER_SPEAKER, Segment
from .sessions import _segment


def _live_person(c: sqlite3.Connection, pid: int) -> sqlite3.Row | None:
    """The person ``pid`` is now (following merges), or None."""
    for _ in range(50):
        row = c.execute("SELECT id, name, is_owner, merged_into FROM people WHERE id = ?",
                        (pid,)).fetchone()
        if row is None or row["merged_into"] is None:
            return row
        pid = row["merged_into"]
    return None


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _refresh_voiceprint(c: sqlite3.Connection, speaker_id: int) -> None:
    vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in c.execute(
        "SELECT embedding FROM voice_samples WHERE speaker_id = ? ORDER BY id", (speaker_id,))]
    # samples of another speaker model (CONVO_SPEAKER_EMBEDDER changed) have another
    # size: the voiceprint follows the newest sample's model
    vecs = [v for v in vecs if vecs and v.size == vecs[-1].size]
    if vecs:
        c.execute("UPDATE speakers SET embedding = ? WHERE id = ?",
                  (_unit(np.mean(vecs, axis=0)).tobytes(), speaker_id))


def _is_owner(c: sqlite3.Connection, name: str, previous: str | None = None) -> int:
    """1 if ``name`` (or ``previous``, when renaming) is the enrolled owner."""
    row = c.execute("SELECT 1 FROM speakers WHERE is_owner = 1 AND (name = ? OR name = ?)",
                    (name, previous or name)).fetchone()
    return int(row is not None)


def _lower(names: Iterable[str]) -> set[str]:
    return {n.strip().lower() for n in names if n and n.strip()}


def _person_id(c: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a name or alias (any case) to a person id, following ``merged_into``.
    A person who is not merged wins over a merged one, a name over an alias."""
    name = (name or "").strip()
    if not name:
        return None
    row = c.execute(
        "SELECT id, merged_into FROM ("
        " SELECT id, merged_into, 0 AS pri FROM people WHERE name = ?"
        " UNION ALL SELECT p.id, p.merged_into, 1 FROM people_aliases a"
        " JOIN people p ON p.id = a.person_id WHERE a.alias = ?)"
        " ORDER BY merged_into IS NOT NULL, pri, id LIMIT 1", (name, name)).fetchone()
    if row is None:
        return None
    pid, seen = row["id"], set()
    while row is not None and row["merged_into"] is not None and pid not in seen:
        seen.add(pid)
        pid = row["merged_into"]
        row = c.execute("SELECT id, merged_into FROM people WHERE id = ?", (pid,)).fetchone()
    return pid


def _voice_of(c: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The speaker row (id, name) of ``name``: the person's linked voice first, else a
    speaker called that in any case (people names are NOCASE, speaker names are not)."""
    pid = _person_id(c, name)
    if pid is not None:
        row = c.execute("SELECT s.id, s.name FROM people p JOIN speakers s"
                        " ON s.id = p.speaker_id WHERE p.id = ?", (pid,)).fetchone()
        if row is not None:
            return row
    return c.execute("SELECT id, name FROM speakers WHERE name = ? COLLATE NOCASE"
                     " ORDER BY name = ? DESC, id LIMIT 1", (name, name)).fetchone()


def _ensure_person(c: sqlite3.Connection, name: str | None, speaker_id: int | None = None,
                   is_owner: bool = False, created_at: float | None = None) -> int | None:
    """Person id for ``name`` (see ``Store.ensure_person``); inserts a row if needed."""
    name = (name or "").strip()
    if not name or _PLACEHOLDER_SPEAKER.match(name):
        return None
    pid = _person_id(c, name)
    if pid is None:
        pid = c.execute("INSERT INTO people (name, created_at) VALUES (?, ?)",
                        (name, created_at if created_at is not None else time.time())
                        ).lastrowid
    if speaker_id is not None:
        c.execute("UPDATE people SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL",
                  (speaker_id, pid))
    if is_owner:
        c.execute("UPDATE people SET is_owner = (id = ?)", (pid,))
    return pid


def _add_alias(c: sqlite3.Connection, pid: int, alias: str | None) -> None:
    alias = (alias or "").strip()
    if alias and not _PLACEHOLDER_SPEAKER.match(alias):
        c.execute("INSERT OR IGNORE INTO people_aliases SELECT id, ? FROM people"
                  " WHERE id = ? AND name != ?", (alias, pid, alias))


def _rename_person(c: sqlite3.Connection, old: str, new: str) -> None:
    """``old`` is now called ``new`` (rename_everywhere): the person keeps their id and
    ``old`` becomes an alias. If ``new`` is already another person, the old person is
    merged into them (``merged_into``, aliases and voice move) and that id survives."""
    src, dst = _person_id(c, old), _person_id(c, new)
    if src is None:  # a name nobody had a row for: it becomes an alias of ``new``
        src = dst if dst is not None else _ensure_person(c, new)
        if src is None:
            return
    target = dst if dst is not None else src
    if target != src:
        before = c.execute("SELECT name, speaker_id, is_owner FROM people WHERE id = ?",
                           (src,)).fetchone()
        c.execute("UPDATE people SET merged_into = ? WHERE id = ? OR merged_into = ?",
                  (target, src, src))
        c.execute("INSERT OR IGNORE INTO people_aliases SELECT ?, alias FROM people_aliases"
                  " WHERE person_id = ?", (target, src))
        c.execute("DELETE FROM people_aliases WHERE person_id = ?", (src,))
        c.execute("UPDATE people SET speaker_id = NULL, is_owner = 0 WHERE id = ?", (src,))
        c.execute("UPDATE people SET speaker_id = COALESCE(speaker_id, ?),"
                  " is_owner = MAX(is_owner, ?) WHERE id = ?",
                  (before["speaker_id"], before["is_owner"], target))
        _add_alias(c, target, before["name"])
    current = c.execute("SELECT name FROM people WHERE id = ?", (target,)).fetchone()["name"]
    c.execute("DELETE FROM people_aliases WHERE person_id = ? AND alias = ?", (target, new))
    c.execute("UPDATE people SET name = ? WHERE id = ?", (new.strip(), target))
    _add_alias(c, target, current)
    _add_alias(c, target, old)


def _touch_conversations(c: sqlite3.Connection, conv_ids: Iterable[str],
                         person: str | None) -> None:
    """Mark conversations for re-summary (speaker names changed); note who took part."""
    now = time.time()
    for conv_id in conv_ids:
        if person and not _PLACEHOLDER_SPEAKER.match(person):
            c.execute("INSERT OR IGNORE INTO conversation_people VALUES (?, ?)",
                      (conv_id, person))
            _ensure_person(c, person)
        c.execute("UPDATE conversations SET status = 'pending', updated_at = ? WHERE id = ?",
                  (now, conv_id))


def _prune_candidates(c: sqlite3.Connection, sessions: Iterable[str]) -> None:
    """Forget unnamed-speaker voices whose label no longer appears in its session."""
    for sid in sessions:
        c.execute("DELETE FROM speaker_candidates WHERE session_id = ? AND label NOT IN"
                  " (SELECT DISTINCT speaker FROM segments WHERE session_id = ?)", (sid, sid))


class PeopleRepo:
    """``Store``'s methods for people and speaker names. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    def speaker_stats(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT speaker AS name, MAX(is_owner) AS is_owner, COUNT(*) AS lines,"
            " COUNT(DISTINCT conversation_id) AS conversations, MAX(started_at) AS last_heard"
            " FROM segments WHERE conversation_id IS NOT NULL GROUP BY speaker COLLATE NOCASE"
            " ORDER BY last_heard DESC")

    def mentioned_people(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT p.name, COUNT(*) AS conversations, MAX(c.started_at) AS last_mentioned"
            " FROM conversation_people p JOIN conversations c ON c.id = p.conversation_id"
            " GROUP BY p.name ORDER BY last_mentioned DESC")

    def known_names(self) -> tuple[set[str], set[str]]:
        """(owner names, person names), lower-cased: enrolled speakers marked as the
        owner, and every enrolled speaker or person named in a conversation."""
        owners: set[str] = set()
        people: set[str] = set()
        for r in self._q("SELECT name, is_owner FROM speakers"
                         " UNION SELECT name, 0 FROM conversation_people"
                         " UNION SELECT name, 0 FROM people"
                         " UNION SELECT alias, 0 FROM people_aliases"):
            name = (r["name"] or "").strip().lower()
            people.add(name)
            if r["is_owner"]:
                owners.add(name)
        return owners - {""}, people - {""}

    def put_speaker_candidates(self, session_id: str, centroids: dict[str, np.ndarray]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM speaker_candidates WHERE session_id = ?", (session_id,))
            c.executemany("INSERT INTO speaker_candidates VALUES (?, ?, ?)",
                          [(session_id, label, np.asarray(v, dtype=np.float32).tobytes())
                           for label, v in centroids.items()])

    def rename_speaker(self, conv_id: str, label: str, name: str) -> tuple[int, np.ndarray | None]:
        """Rename ``label`` to ``name`` within one conversation.

        Returns (lines changed, averaged voice embedding of that label if known). The
        conversation is marked pending so the worker re-summarizes it with the real name.
        """
        with self.tx() as c:
            sessions = [r["session_id"] for r in c.execute(
                "SELECT DISTINCT session_id FROM segments WHERE conversation_id = ?"
                " AND speaker = ?", (conv_id, label))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE"
                          " conversation_id = ? AND speaker = ?",
                          (name, _is_owner(c, name), conv_id, label)).rowcount
            vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in c.execute(
                f"SELECT embedding FROM speaker_candidates WHERE label = ? AND session_id IN"
                f" ({','.join('?' * len(sessions))})", (label, *sessions))] if sessions else []
            if n:
                _touch_conversations(c, [conv_id], name)
            _prune_candidates(c, sessions)
        return n, (np.mean(vecs, axis=0) if vecs else None)

    def unnamed_candidates(self) -> list[dict[str, Any]]:
        """Unnamed speakers ("Speaker N") with a stored voice, per session, still in use."""
        rows = self._q(
            "SELECT k.session_id, k.label, k.embedding, COUNT(s.id) AS lines,"
            " GROUP_CONCAT(DISTINCT s.conversation_id) AS conversations,"
            " MIN(s.started_at) AS first_at"
            " FROM speaker_candidates k JOIN segments s ON s.session_id = k.session_id"
            " AND s.speaker = k.label AND s.conversation_id IS NOT NULL"
            " GROUP BY k.session_id, k.label ORDER BY first_at DESC")
        for r in rows:
            r["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            r["conversations"] = (r["conversations"] or "").split(",")
        return rows

    def rename_session_label(self, session_id: str, label: str, name: str) -> tuple[int, list[str]]:
        """Rename an unnamed label within one recording session (all its conversations)."""
        with self.tx() as c:
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM segments WHERE session_id = ?"
                " AND speaker = ? AND conversation_id IS NOT NULL", (session_id, label))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE session_id = ?"
                          " AND speaker = ?", (name, _is_owner(c, name), session_id,
                                               label)).rowcount
            _touch_conversations(c, convs, name)
            _prune_candidates(c, [session_id])
        return n, convs

    def rename_everywhere(self, old: str, new: str) -> tuple[int, list[str]]:
        """Rename a speaker in every conversation and merge their voice into ``new``.

        Returns (lines changed, conversation ids). Unnamed "Speaker N" labels are
        per recording, so callers should only pass real names. The person keeps their id
        (``old`` becomes an alias); if ``new`` is already a person, the old person is
        merged into that one, and the id of the name that already existed survives.
        """
        with self.tx() as c:
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM segments WHERE speaker = ?"
                " COLLATE NOCASE AND conversation_id IS NOT NULL", (old,))]
            n = c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE speaker = ?"
                          " COLLATE NOCASE", (new, _is_owner(c, new, old), old)).rowcount
            mentioned = c.execute("UPDATE OR REPLACE conversation_people SET name = ?"
                                  " WHERE name = ?", (new, old)).rowcount
            src = c.execute("SELECT id, is_owner FROM speakers WHERE name = ? COLLATE NOCASE",
                            (old,)).fetchone()
            dst = c.execute("SELECT id FROM speakers WHERE name = ?", (new,)).fetchone()
            if src is not None and (dst is None or dst["id"] == src["id"]):
                c.execute("UPDATE speakers SET name = ? WHERE id = ?", (new, src["id"]))
            elif src is not None:  # both known: the old voice samples join the new name
                c.execute("UPDATE voice_samples SET speaker_id = ? WHERE speaker_id = ?",
                          (dst["id"], src["id"]))
                if src["is_owner"]:
                    c.execute("UPDATE speakers SET is_owner = 1 WHERE id = ?", (dst["id"],))
                c.execute("UPDATE people SET speaker_id = NULL WHERE speaker_id = ?",
                          (src["id"],))
                c.execute("DELETE FROM speakers WHERE id = ?", (src["id"],))
                _refresh_voiceprint(c, dst["id"])
            if n or mentioned or src is not None:
                _rename_person(c, old, new)
            _touch_conversations(c, convs, None)
        return n, convs

    def get_segment(self, segment_id: int) -> Segment | None:
        row = self._one("SELECT * FROM segments WHERE id = ?", (segment_id,))
        return _segment(row) if row else None

    def reassign_segment(self, segment_id: int, name: str) -> Segment | None:
        """Give one transcript line to ``name``; returns the line as it was before."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE segments SET speaker = ?, is_owner = ? WHERE id = ?",
                      (name, _is_owner(c, name), segment_id))
            if row["conversation_id"]:
                _touch_conversations(c, [row["conversation_id"]], name)
            return _segment(dict(row))

    def owner_name(self) -> str | None:
        """The enrolled owner voice's name, or None; callers then use CONVO_OWNER_NAME.
        This is the owner's name on every owner line (meetings, voice notes, written
        notes). A ``people`` row never names the owner: a Zoom name matched by
        CONVO_ZOOM_OWNER_NAMES is only an alias (owner request, QA BUG-07)."""
        row = self._one("SELECT name FROM speakers WHERE is_owner = 1")
        return row["name"] if row else None

    def owner_person_name(self) -> str | None:
        """The current name of the owner's person (``owner_person_id``), or None."""
        row = self._one("SELECT name FROM people WHERE is_owner = 1 AND merged_into IS NULL")
        return row["name"] if row else None

    def name_owner(self, name: str) -> None:
        """Call the owner's person ``name`` (the canonical owner name: the enrolled owner
        voice's, else CONVO_OWNER_NAME). The person keeps their id and the old name (for
        example a Zoom display name a meeting named the owner by before) becomes an
        alias; owner lines under the old name move to ``name`` and stay owner lines. If
        ``name`` is already another person, the owner is merged into them (that id
        survives and becomes the owner). No-op without an owner person."""
        name = (name or "").strip()
        if not name or _PLACEHOLDER_SPEAKER.match(name):
            return
        with self.tx() as c:
            row = c.execute("SELECT name FROM people WHERE is_owner = 1"
                            " AND merged_into IS NULL").fetchone()
            if row is None or row["name"] == name:
                return
            old = row["name"]
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM segments WHERE speaker = ?"
                " COLLATE NOCASE AND is_owner = 1 AND conversation_id IS NOT NULL", (old,))]
            c.execute("UPDATE segments SET speaker = ? WHERE speaker = ? COLLATE NOCASE"
                      " AND is_owner = 1", (name, old))
            c.execute("UPDATE OR REPLACE conversation_people SET name = ? WHERE name = ?",
                      (name, old))
            _rename_person(c, old, name)
            _touch_conversations(c, convs, None)

    # ------------------------------------------------------------------ people
    def ensure_person(self, name: str, speaker_id: int | None = None,
                      is_owner: bool = False) -> int | None:
        """The id of the person called ``name`` (name or alias, any case), created if new.

        None for an empty name or an unnamed label ("Speaker 1"). ``speaker_id`` links an
        enrolled voice when the person has none yet; ``is_owner`` makes them the owner.
        """
        with self.tx() as c:
            return _ensure_person(c, name, speaker_id, is_owner)

    def link_person(self, names: Iterable[str], aliases: Iterable[str] = (),
                    is_owner: bool = False) -> tuple[int, str] | None:
        """The first of ``names`` that is a person (name or alias, any case, following
        merges), else a new person called the first usable one; ``aliases`` are added to
        that person (its own name and unnamed labels are skipped). Returns (id, current
        name), or None when no name is usable. One transaction (meeting_names.py)."""
        names = [n.strip() for n in names if n and n.strip()
                 and not _PLACEHOLDER_SPEAKER.match(n.strip())]
        if not names:
            return None
        with self.tx() as c:
            pid = next((p for p in (_person_id(c, n) for n in names) if p is not None),
                       None)
            if pid is None:
                pid = _ensure_person(c, names[0])
            if is_owner:
                c.execute("UPDATE people SET is_owner = (id = ?)", (pid,))
            for alias in aliases:
                _add_alias(c, pid, alias)
            return pid, c.execute("SELECT name FROM people WHERE id = ?",
                                  (pid,)).fetchone()["name"]

    def person_id(self, name: str, owner_names: Iterable[str] = ()) -> int | None:
        """The id of the person with this name or alias (any case), following merges.

        A name in ``owner_names`` (the words that mean the owner, see
        ``owner_person_id``) resolves to the owner's person when there is one.
        """
        if (name or "").strip().lower() in _lower(owner_names):
            owner = self.owner_person_id(owner_names)
            if owner is not None:
                return owner
        with self._lock:
            return _person_id(self._conn, name)

    def owner_person_id(self, owner_names: Iterable[str] = ()) -> int | None:
        """The owner's person: the one with the enrolled owner voice; without one, the
        first person whose name or alias is in ``owner_names`` (the configured owner
        name first, then words like "me"). This is the single source of the owner."""
        row = self._one("SELECT id FROM people WHERE is_owner = 1 AND merged_into IS NULL")
        if row is not None:
            return row["id"]
        with self._lock:
            for name in owner_names:
                pid = _person_id(self._conn, name)
                if pid is not None:
                    return pid
        return None

    def conversation_participants(self, conv_ids: Iterable[str],
                                  owner_names: Iterable[str] = ()
                                  ) -> dict[str, list[dict[str, Any]]]:
        """For each conversation, the people in it other than the owner, in order of
        first appearance: named speakers of its lines (by first line), then the people
        its summary lists. Each is ``{"id", "name", "names"}``: the person's id (following
        merges; None for a name with no person row), their current name, and every name
        they appear under here (for matching against text). Unnamed labels ("Speaker 2",
        "Unknown") are skipped, and so is the owner: owner lines, the owner's person (see
        ``owner_person_id``), its aliases, and any name in ``owner_names``. One pass for
        the whole set; names are resolved once each."""
        conv_ids = list(dict.fromkeys(conv_ids))
        out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in conv_ids}
        if not conv_ids:
            return out
        owner_names = tuple(owner_names)
        owner = self.owner_person_id(owner_names)
        marks = ",".join("?" * len(conv_ids))
        rows = self._q(
            "SELECT conversation_id AS conv, speaker AS name, MIN(started_at) AS first,"
            " 0 AS src FROM segments WHERE is_owner = 0 AND conversation_id IN"
            f" ({marks}) GROUP BY conversation_id, speaker COLLATE NOCASE"
            " UNION ALL SELECT conversation_id, name, rowid, 1 FROM conversation_people"
            f" WHERE conversation_id IN ({marks}) ORDER BY 1, 4, 3", [*conv_ids, *conv_ids])
        skip = _lower(owner_names) | {r["name"].strip().lower() for r in self._q(
            "SELECT name FROM speakers WHERE is_owner = 1")}
        if owner is not None:
            skip |= {r["name"].strip().lower() for r in self._q(
                "SELECT name FROM people WHERE id = ? UNION SELECT alias FROM people_aliases"
                " WHERE person_id = ?", (owner, owner))}
        resolved: dict[str, tuple[int | None, str]] = {}
        with self._lock:
            for r in rows:
                key = (r["name"] or "").strip().lower()
                if key in resolved or not key or key in skip or _PLACEHOLDER_SPEAKER.match(key):
                    continue
                pid = _person_id(self._conn, r["name"])
                row = (self._conn.execute("SELECT name FROM people WHERE id = ?", (pid,))
                       .fetchone() if pid is not None else None)
                resolved[key] = (pid, row["name"] if row else r["name"].strip())
        for r in rows:
            key = (r["name"] or "").strip().lower()
            if key not in resolved or resolved[key][0] == owner and owner is not None:
                continue
            pid, name = resolved[key]
            entries = out[r["conv"]]
            same = next((e for e in entries if (e["id"] == pid if pid is not None
                                                else e["name"].lower() == key)), None)
            if same is None:
                entries.append({"id": pid, "name": name, "names": [name]})
                same = entries[-1]
            if r["name"].strip().lower() not in (n.lower() for n in same["names"]):
                same["names"].append(r["name"].strip())
        return out

    def people(self, owner_names: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Every person not merged into another: id, name, aliases, is_owner, has_voice,
        conversations (distinct conversations they spoke in or were named in, under their
        name or an alias), first_at and last_at (start of the first and last of those).

        ``is_owner`` is true for ``owner_person_id(owner_names)`` only; other people
        called by an owner name ("Me" next to an enrolled owner "Sam") are listed as
        that owner (their names become the owner's aliases), not as people of their own.
        """
        owner_names = tuple(owner_names)
        owner = self.owner_person_id(owner_names)
        rows = self._q("SELECT id, name, is_owner, speaker_id IS NOT NULL AS has_voice,"
                       " merged_into FROM people ORDER BY id")
        survivor = {r["id"]: r["merged_into"] for r in rows}
        with self._lock:
            fold = {_person_id(self._conn, n) for n in owner_names} - {None, owner}
        if owner is None:
            fold = set()

        def live(pid: int) -> int:
            for _ in range(len(survivor)):
                if survivor.get(pid) is None:
                    break
                pid = survivor[pid]
            return owner if pid in fold else pid

        out = {r["id"]: {"id": r["id"], "name": r["name"], "aliases": [],
                         "is_owner": r["id"] == owner, "has_voice": bool(r["has_voice"]),
                         "_convs": {}}
               for r in rows if r["merged_into"] is None and r["id"] not in fold}
        by_name: dict[str, int] = {}
        for r in sorted(rows, key=lambda r: r["merged_into"] is None):  # live names win
            by_name[r["name"].lower()] = live(r["id"])
        aliases = [(r["person_id"], r["alias"]) for r in self._q(
            "SELECT person_id, alias FROM people_aliases")]
        aliases += [(r["id"], r["name"]) for r in rows if r["id"] in fold]
        for person_id, alias in sorted(aliases, key=lambda a: a[1].lower()):
            pid = live(person_id)
            by_name.setdefault(alias.lower(), pid)
            if pid in out and alias.lower() not in (
                    a.lower() for a in [out[pid]["name"], *out[pid]["aliases"]]):
                out[pid]["aliases"].append(alias)
        for r in self._q(
                "SELECT s.speaker AS name, c.id, c.started_at FROM segments s"
                " JOIN conversations c ON c.id = s.conversation_id"
                " GROUP BY s.speaker, c.id"
                " UNION SELECT p.name, c.id, c.started_at FROM conversation_people p"
                " JOIN conversations c ON c.id = p.conversation_id"):
            pid = by_name.get((r["name"] or "").strip().lower())
            if pid in out:
                out[pid]["_convs"][r["id"]] = r["started_at"]
        for p in out.values():
            starts = p.pop("_convs").values()
            p["conversations"] = len(starts)
            p["first_at"] = min(starts, default=None)
            p["last_at"] = max(starts, default=None)
        return list(out.values())
