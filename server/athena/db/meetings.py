"""Meeting bots, meeting tracks, live transcription state and meeting info."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from ..domain import _ACTIVE_IN, CHAT, MEETING_ACTIVE, Segment, _device_name
from .people import _live_person, _person_id


class MeetingsRepo:
    """``Store``'s methods for meetings. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    # ---------------------------------------------------------------- meetings
    def add_meeting(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        with self.tx() as c:
            c.execute(f"INSERT INTO meetings ({cols}) VALUES ({', '.join('?' * len(row))})",
                      tuple(row.values()))

    def update_meeting(self, meeting_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE meetings SET {sets} WHERE id = ?", (*fields.values(), meeting_id))

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))

    def list_meetings(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?", (limit,))

    def active_meetings(self) -> list[dict[str, Any]]:
        return self._q(f"SELECT * FROM meetings WHERE state IN ({_ACTIVE_IN})"
                       " ORDER BY created_at", MEETING_ACTIVE)

    def meeting_upload_session(self, token: str) -> str | None:
        """The session a running meeting bot may upload to with ``token``, else None.
        Only bots that are still active hold a token (its hash; cleared on exit)."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self._one("SELECT session_id FROM meetings WHERE upload_token_hash = ?"
                        f" AND state IN ({_ACTIVE_IN})", (digest, *MEETING_ACTIVE))
        return row["session_id"] if row else None

    def meeting_conversations(self, conv_ids: Iterable[str]) -> set[str]:
        """Those of ``conv_ids`` that hold lines of a session with tracks (a meeting)."""
        ids = list(conv_ids)
        if not ids:
            return set()
        return {r["conversation_id"] for r in self._q(
            "SELECT DISTINCT s.conversation_id FROM segments s WHERE s.conversation_id IN"
            f" ({','.join('?' * len(ids))}) AND EXISTS (SELECT 1 FROM chunks k"
            " WHERE k.session_id = s.session_id AND k.track != '')", ids)}

    def tracks(self, session_id: str) -> list[dict[str, Any]]:
        """The session's tracks (chunks without a track are not one), ordered by first
        chunk time: ``{"track", "name", "names", "chunks", "started_at", "ended_at"}``.
        ``name`` is the raw display name of the track's latest chunk in time (by
        ``started_at``, then sequence) that had one, else None; ``names`` every distinct
        raw name seen, in order of first use in time, so a participant who renames
        mid-meeting stays one track with several names. Names are stored as sent: a
        later name replaces an earlier one even if a cleaner would drop it (an emoji,
        "iPhone"), so callers that clean names should check all of ``names``."""
        out: dict[str, dict[str, Any]] = {}
        for r in self._q(
                "SELECT track, track_name, sequence, started_at, duration FROM chunks"
                " WHERE session_id = ? AND track != '' ORDER BY track, started_at, sequence",
                (session_id,)):
            t = out.setdefault(r["track"], {"track": r["track"], "name": None, "names": [],
                                            "chunks": 0, "started_at": r["started_at"],
                                            "ended_at": r["started_at"] + r["duration"]})
            t["chunks"] += 1
            t["started_at"] = min(t["started_at"], r["started_at"])
            t["ended_at"] = max(t["ended_at"], r["started_at"] + r["duration"])
            name = r["track_name"]
            if name:
                t["name"] = name
                if name not in t["names"]:
                    t["names"].append(name)
        return sorted(out.values(), key=lambda t: (t["started_at"], t["track"]))

    # ---------------------------------------------------------------- meetings
    def live_meetings(self) -> list[str]:
        """Meetings being recorded (status 'recording' with a tracked chunk), oldest first.
        The worker asks this on every tick (meetings/transcribe/live.py): one query on the status
        index, and each candidate's tracked chunk is found on the chunks key."""
        return [r["id"] for r in self._q(
            "SELECT s.id FROM sessions s WHERE s.status = 'recording' AND EXISTS"
            " (SELECT 1 FROM chunks k WHERE k.session_id = s.id AND k.track > '')"
            " ORDER BY s.started_at")]

    def live_states(self, session_id: str) -> dict[str, str]:
        """The live transcription state (JSON) of each track of a meeting."""
        return {r["track"]: r["state"] for r in self._q(
            "SELECT track, state FROM meeting_live WHERE session_id = ?", (session_id,))}

    def put_live_state(self, session_id: str, track: str, state: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meeting_live (session_id, track, state, updated_at)"
                      " VALUES (?, ?, ?, ?) ON CONFLICT(session_id, track) DO UPDATE SET"
                      " state = excluded.state, updated_at = excluded.updated_at",
                      (session_id, track, state, time.time()))

    def drop_live_states(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM meeting_live WHERE session_id = ?", (session_id,))

    def put_meeting_chat(self, session_id: str, lines: list[tuple[str, Segment]],
                         topic: str | None = None,
                         participants: list[dict[str, Any]] | None = None,
                         now: float | None = None) -> dict[str, Any]:
        """Store a meeting's chat lines and its topic and participants, in one transaction
        (``POST /v1/sessions/{id}/chat``, ``meetings/ingest/chat.py``).

        ``lines`` are (line_key, segment): a key already stored for the session is a retry
        and is skipped. New lines become segments of kind CHAT without a conversation;
        the caller attaches them. ``topic`` / ``participants`` replace the stored ones
        when given (None keeps them). A session being processed is sent back for another
        pass when something changed, so the new lines join its conversation. Returns
        ``{"added": [segment ids], "duplicates", "changed" (topic or participants),
        "status" (the session's, before this call), "device"}``; KeyError for an unknown
        session.
        """
        now = time.time() if now is None else now
        with self.tx() as c:
            sess = c.execute("SELECT device, status FROM sessions WHERE id = ?",
                             (session_id,)).fetchone()
            if sess is None:
                raise KeyError(session_id)
            added: list[int] = []
            duplicates = 0
            for key, seg in lines:
                if not c.execute("INSERT OR IGNORE INTO meeting_chat VALUES (?, ?)",
                                 (session_id, key)).rowcount:
                    duplicates += 1
                    continue
                added.append(c.execute(
                    "INSERT INTO segments (session_id, device, started_at, ended_at, speaker,"
                    " is_owner, text, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, sess["device"], seg.started_at, seg.ended_at, seg.speaker,
                     int(seg.is_owner), seg.text, CHAT)).lastrowid)
            info = c.execute("SELECT topic, participants FROM meeting_info WHERE"
                             " session_id = ?", (session_id,)).fetchone()
            old_topic, old_parts = (info["topic"], info["participants"]) if info else (None,
                                                                                       "[]")
            new_topic = old_topic if topic is None else topic
            new_parts = old_parts if participants is None else json.dumps(participants)
            changed = (new_topic, new_parts) != (old_topic, old_parts)
            c.execute("INSERT INTO meeting_info (session_id, topic, participants, updated_at)"
                      " VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
                      " topic = excluded.topic, participants = excluded.participants,"
                      " updated_at = CASE WHEN ? THEN excluded.updated_at ELSE updated_at END",
                      (session_id, new_topic, new_parts, now, int(changed)))
            if sess["status"] == "processing" and (added or changed):
                c.execute("UPDATE sessions SET status = 'ended' WHERE id = ?"
                          " AND status = 'processing'", (session_id,))
            return {"added": added, "duplicates": duplicates, "changed": changed,
                    "status": sess["status"], "device": sess["device"]}

    def link_meeting(self, session_id: str, conv_id: str) -> None:
        """Record ``conv_id`` as the conversation of the meeting ``session_id`` and index
        its search entry again, so a topic, participants or chat lines sent late are found
        before the summary is redone (QA BUG-14)."""
        with self.tx() as c:
            old = c.execute("SELECT conversation_id FROM meeting_info WHERE session_id = ?",
                            (session_id,)).fetchone()
            c.execute("INSERT INTO meeting_info (session_id, conversation_id, updated_at)"
                      " VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
                      " conversation_id = excluded.conversation_id",
                      (session_id, conv_id, time.time()))
            for cid in {conv_id, *([old[0]] if old and old[0] else [])}:
                c.execute("DELETE FROM search_fts WHERE kind = 'transcript'"
                          " AND conversation_id = ?", (cid,))
                if c.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
                    self._index_transcript(c, cid)

    def has_meeting_info(self, session_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM meeting_info WHERE session_id = ?",
                         (session_id,)) is not None

    def meeting_conversation(self, session_id: str) -> str | None:
        """The meeting's conversation: the linked one, else the one holding its lines."""
        row = self._one("SELECT m.conversation_id AS id FROM meeting_info m JOIN"
                        " conversations c ON c.id = m.conversation_id"
                        " WHERE m.session_id = ?", (session_id,))
        if row:
            return row["id"]
        have = self.session_conversations(session_id)
        return have[0] if have else None

    def meetings_for(self, conv_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``{conversation_id: {"topic", "participants"}}`` for the Zoom meetings among
        ``conv_ids`` (others are left out). ``topic`` is the Zoom topic or None;
        ``participants`` ``[{"name", "person_id", "is_owner", "zoom_name", "joined_at",
        "left_at"}]``: the participant list the bot sent, each named by their person's
        current name, then anyone else heard or seen in its chat, in order of first line
        (``person_id``, ``zoom_name`` and the times are None for those)."""
        with self._lock:
            return self._meetings_for(self._conn, list(dict.fromkeys(conv_ids)))

    def _meetings_for(self, c: sqlite3.Connection,
                      ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in c.execute(
                    "SELECT conversation_id, topic, participants FROM meeting_info"
                    f" WHERE conversation_id IN ({','.join('?' * len(part))})"
                    " ORDER BY updated_at", part).fetchall():
                m = out.setdefault(r["conversation_id"], {"topic": None, "participants": []})
                m["topic"] = m["topic"] or r["topic"]
                m["participants"] += json.loads(r["participants"] or "[]")
        for conv_id, m in out.items():
            people: list[dict[str, Any]] = []
            keys: set[tuple[Any, ...]] = set()  # one entry per zoom_user_id (QA BUG-13)
            seen: set[str] = set()  # names listed, so heard speakers aren't listed twice
            devices: list[dict[str, Any]] = []  # shown only as a device or room
            for p in m["participants"]:
                name, owner = p.get("name") or p.get("zoom_name") or "", False
                pid = p.get("person_id")
                if pid is not None:
                    live = _live_person(c, pid)
                    if live is not None:
                        pid, name, owner = live["id"], live["name"], bool(live["is_owner"])
                zid = p.get("zoom_user_id")
                # two people with one name ("Alex", "iPhone") are two entries
                key = ("id", zid) if zid else ("name", name.casefold(), p.get("joined_at"))
                if not name or key in keys:
                    continue
                keys.add(key)
                seen.add(name.casefold())
                entry = {"name": name, "person_id": pid, "is_owner": owner,
                         "zoom_name": p.get("zoom_name"),
                         "joined_at": p.get("joined_at"), "left_at": p.get("left_at")}
                people.append(entry)
                if pid is None and not owner and _device_name(p.get("zoom_name") or name):
                    devices.append(entry)
            heard, unnamed = [], []  # unnamed: "Speaker 1", a room: no person
            for r in c.execute("SELECT speaker, MAX(is_owner) AS is_owner, MIN(started_at)"
                               " AS first FROM segments WHERE conversation_id = ?"
                               " GROUP BY speaker COLLATE NOCASE ORDER BY first",
                               (conv_id,)).fetchall():
                if r["speaker"].casefold() not in seen:
                    seen.add(r["speaker"].casefold())
                    heard.append({"name": r["speaker"], "person_id": None,
                                  "is_owner": bool(r["is_owner"]), "zoom_name": None,
                                  "joined_at": None, "left_at": None})
                    if not r["is_owner"] and _person_id(c, r["speaker"]) is None:
                        unnamed.append(heard[-1])
            if unnamed and devices:
                # A participant Zoom shows only as a device or room ("iPhone") whose lines
                # are named "Speaker 1" (no person) is listed by those speakers, not
                # twice; with one such participant they keep its Zoom name and times.
                people = [p for p in people if p not in devices]
                if len(devices) == 1:
                    for h in unnamed:
                        h.update({k: devices[0][k] for k in ("zoom_name", "joined_at",
                                                             "left_at")})
            m["participants"] = people + heard
        return out
