"""A meeting's chat, topic and participants.

The bot sends them in one call when the meeting ends: ``POST /v1/sessions/{id}/chat``.

* Each chat message becomes a transcript line of kind ``chat`` at the time it was sent,
  from its sender's person: the sender's track (``zoom_user_id``) is named exactly as
  that track's spoken lines are (``meeting_tracks.track_speaker``), and a sender without
  a track by their chat name (``meeting_names.speaker_for_track``). No VAD, no whisper.
* Retries are free: a line is stored once per session (SHA-256 of its cleaned time,
  sender id, sender name and text), also after ``erase_fact`` redacted it or retention
  removed it.
* Private messages (``"private": true``, sent to Athena alone) are dropped unread.
* Order doesn't matter. Before the meeting is processed the lines wait in the session
  and join its conversation then; while it is processed the session is sent back for one
  more pass; after, they are added to its conversation, which is summarized again.
* The topic and participant list show on the meeting's conversation (MCP
  ``(Online meeting: <topic>)``) and go into its summary prompt (``summary_context``).

Imported only by the chat route and, for a meeting's conversation, by the summary step,
so a server that never sees a meeting never loads it.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .pipeline import MAX_TITLE_CHARS, MAX_TRACK_NAME_CHARS, TRACK, validate_id
from .store import CHAT, ZOOM_DEVICE, Segment

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

MAX_CHAT_BYTES = 1_000_000  # request body
MAX_CHAT_LINES = 2_000
MAX_CHAT_LINE_CHARS = 4_000  # longer messages are cut
MAX_PARTICIPANTS = 1_000
# times must be plausible: 2000-01-01 .. 2100-01-01 UTC
_EARLIEST, _LATEST = 946_684_800.0, 4_102_444_800.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# One chat call at a time adds lines to a finished meeting's conversation, so two calls
# can't each make one.
_ATTACH = threading.Lock()


class NotAMeeting(Exception):
    """The session is no meeting: it has no tracks and its device is not ``zoom``."""


class TooLarge(Exception):
    """Over a cap: ``MAX_CHAT_BYTES``, ``MAX_CHAT_LINES`` or ``MAX_PARTICIPANTS``."""


class ChatLineIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    at: str | float
    from_: str = Field(default="", alias="from")
    zoom_user_id: str | None = None
    text: str
    private: bool = False


class ParticipantIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    zoom_user_id: str | None = None
    name: str = ""
    joined_at: str | float | None = None
    left_at: str | float | None = None


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lines: list[ChatLineIn] = Field(default_factory=list)
    topic: str | None = None
    participants: list[ParticipantIn] | None = None


def check_size(raw: Any) -> None:
    """Raise TooLarge when the parsed body has too many lines or participants (checked
    before validation, so a huge list isn't validated first)."""
    if not isinstance(raw, dict):
        return
    if isinstance(raw.get("lines"), list) and len(raw["lines"]) > MAX_CHAT_LINES:
        raise TooLarge(f"more than {MAX_CHAT_LINES} chat lines")
    parts = raw.get("participants")
    if isinstance(parts, list) and len(parts) > MAX_PARTICIPANTS:
        raise TooLarge(f"more than {MAX_PARTICIPANTS} participants")


def parse_time(value: str | float | None, what: str) -> float | None:
    """ISO-8601 (naive = UTC) or epoch seconds/milliseconds; None stays None. Raises
    ValueError for anything else or a time outside 2000..2100."""
    if value is None or value == "":
        return None
    num: float | None
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = None
    if num is not None:
        if not math.isfinite(num):
            raise ValueError(f"{what} must be ISO-8601 or epoch seconds")
        ts = num / 1000.0 if num > 1e11 else num
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"{what} must be ISO-8601 or epoch seconds") from e
        ts = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    if not (_EARLIEST <= ts < _LATEST):
        raise ValueError(f"{what} must be a time between 2000 and 2100")
    return ts


def clean_text(text: str, limit: int) -> str:
    """Control characters out, runs of spaces and line breaks as one space, at most
    ``limit`` characters. Links and file names stay as written."""
    return " ".join(_CONTROL.sub(" ", text or "").split())[:limit].rstrip()


def _zoom_id(value: str | None, what: str) -> str:
    v = (value or "").strip()
    if v and not TRACK.match(v):
        raise ValueError(f"{what} must match {TRACK.pattern}")
    return v


def line_key(at: float, zoom_user_id: str, sender: str, text: str) -> str:
    raw = "\x1f".join((f"{at:.3f}", zoom_user_id, sender, text))
    return hashlib.sha256(raw.encode()).hexdigest()


class _Names:
    """Who chat senders and participants are, named as the meeting's tracks are."""

    def __init__(self, pipeline: Pipeline, session_id: str,
                 seen: dict[str, list[str]] | None = None) -> None:
        self.store, cfg = pipeline.store, pipeline.settings
        self.owner_names = cfg.zoom_owner_name_list
        self.owner_display = self.store.owner_name() or cfg.owner_name
        self.tracks = {t["track"]: t for t in self.store.tracks(session_id)}
        # every name a zoom_user_id used in this call (chat senders first, then the
        # participant list, Zoom's latest), so one Zoom user is one person (QA BUG-12)
        self.seen = seen or {}
        self._cache: dict[str, tuple[str, bool]] = {}

    @staticmethod
    def _key(zoom_user_id: str, name: str) -> str:
        return f"id:{zoom_user_id}" if zoom_user_id else f"name:{name}"

    def speaker(self, zoom_user_id: str, name: str) -> tuple[str, bool]:
        """(speaker name, is_owner) for a chat sender: their track's name when they have
        one, else their names in this call as one person (made when new, as for a
        track). Resolved once per zoom_user_id, whichever of its names asks."""
        key = self._key(zoom_user_id, name)
        if key not in self._cache:
            from .meeting_names import speaker_for_track
            from .meeting_tracks import track_speaker

            who, owner = "", False
            if zoom_user_id in self.tracks:
                who, owner = track_speaker(self.store, self.tracks[zoom_user_id],
                                           self.owner_names,
                                           owner_display=self.owner_display)
            seen = self.seen.get(zoom_user_id, []) if zoom_user_id else []
            names = [n for n in dict.fromkeys([*([name] if name not in seen else []), *seen])
                     if n]
            if not who and not owner and names:
                who, owner = speaker_for_track(
                    self.store, {"track": zoom_user_id, "name": names[-1], "names": names,
                                 "placeholder": ""}, self.owner_names,
                    owner_display=self.owner_display)
            if owner:
                who = self.owner_display or who
            elif not who:  # a room, a device or no name: what Zoom showed, else Unknown
                who = (names[-1] if names else "") or "Unknown"
            self._cache[key] = (who, owner)
        return self._cache[key]

    def participant(self, zoom_user_id: str, name: str) -> tuple[str, int | None]:
        """(name, person id or None) for a participant. Someone with a track or a chat
        line was made a person already; someone who never spoke or typed is only looked
        up, so a silent guest adds no person."""
        from .meeting_names import clean_display_name

        if zoom_user_id and (zoom_user_id in self.tracks
                             or self._key(zoom_user_id, name) in self._cache):
            who, owner = self.speaker(zoom_user_id, name)
        else:
            clean = clean_display_name(name)
            owner_keys = {clean_display_name(o).lower() for o in self.owner_names} - {""}
            owner = bool(clean) and clean.lower() in owner_keys
            who = (self.owner_display or clean) if owner else (clean or name)
        if owner:
            return who, self.store.owner_person_id(self.owner_names)
        return who, self.store.person_id(who) if who else None


def _names_by_zoom_id(body: ChatIn) -> dict[str, list[str]]:
    """Every cleaned name each zoom_user_id used in the call: chat senders in line order,
    then the participant list (Zoom's latest name, preferred). Bad ids are skipped here
    and rejected by the main pass."""
    out: dict[str, list[str]] = {}
    pairs = [(ln.zoom_user_id, ln.from_) for ln in body.lines if not ln.private]
    pairs += [(p.zoom_user_id, p.name) for p in body.participants or []]
    for zid, raw in pairs:
        zid, name = (zid or "").strip(), clean_text(raw, MAX_TRACK_NAME_CHARS)
        if zid and name and TRACK.match(zid):
            got = out.setdefault(zid, [])
            if name in got:
                got.remove(name)
            got.append(name)
    return out


def ingest_chat(pipeline: Pipeline, session_id: str, body: ChatIn) -> dict[str, Any]:
    """Store a meeting's chat lines, topic and participants (see the module doc).

    Raises KeyError (unknown session), NotAMeeting, ValueError (a bad field). Returns
    ``{"result": "queued"|"attached"|"unchanged", "added", "duplicates",
    "private_dropped", "conversation_id"}``.
    """
    store = pipeline.store
    validate_id(session_id, "session_id")
    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError(session_id)
    if sess["device"] != ZOOM_DEVICE and not store.has_tracks(session_id):
        raise NotAMeeting(session_id)
    names = _Names(pipeline, session_id, _names_by_zoom_id(body))
    lines: list[tuple[str, Segment]] = []
    private = 0
    for i, ln in enumerate(body.lines):
        if ln.private:  # sent to Athena alone: never stored
            private += 1
            continue
        text = clean_text(ln.text, MAX_CHAT_LINE_CHARS)
        at = parse_time(ln.at, f"lines[{i}].at")
        zid = _zoom_id(ln.zoom_user_id, f"lines[{i}].zoom_user_id")
        sender = clean_text(ln.from_, MAX_TRACK_NAME_CHARS)
        if not text or at is None:
            continue
        who, owner = names.speaker(zid, sender)
        lines.append((line_key(at, zid, sender, text),
                      Segment(started_at=at, ended_at=at, text=text, speaker=who,
                              is_owner=owner, kind=CHAT)))
    topic = None if body.topic is None else (clean_text(body.topic, MAX_TITLE_CHARS) or None)
    participants = None
    if body.participants is not None:
        participants = []
        for i, p in enumerate(body.participants):
            zid = _zoom_id(p.zoom_user_id, f"participants[{i}].zoom_user_id")
            raw = clean_text(p.name, MAX_TRACK_NAME_CHARS)
            joined = parse_time(p.joined_at, f"participants[{i}].joined_at")
            left = parse_time(p.left_at, f"participants[{i}].left_at")
            if not raw and not zid:
                continue
            who, pid = names.participant(zid, raw)
            participants.append({"zoom_user_id": zid or None, "zoom_name": raw or None,
                                 "name": who, "person_id": pid, "joined_at": joined,
                                 "left_at": left})
    lines.sort(key=lambda kl: kl[1].started_at)
    # topic or participants None: keep the stored ones (a resend without them clears
    # nothing)
    out = store.put_meeting_chat(session_id, lines, topic, participants)
    result = "unchanged" if not (out["added"] or out["changed"]) else "queued"
    conv_id = None
    if result == "queued" and out["status"] == "done":
        with _ATTACH:
            conv_id = store.meeting_conversation(session_id)
            if conv_id is not None or out["added"]:
                conv_id = store.save_conversation(conv_id, out["device"], out["added"])
                store.link_meeting(session_id, conv_id)
                result = "attached"  # summarized again on the worker's next pass
    elif out["status"] == "done":
        conv_id = store.meeting_conversation(session_id)
    return {"result": result, "added": len(out["added"]), "duplicates": out["duplicates"],
            "private_dropped": private, "conversation_id": conv_id}


def summary_context(meeting: dict[str, Any], segments: list[Segment]) -> str:
    """What the summary model is told about a meeting before its transcript."""
    topic = meeting.get("topic")
    out = ["This is an online meeting" + (f' with the topic "{topic}"' if topic else "") + "."]
    people = [p["name"] for p in meeting.get("participants") or []]
    if people:
        out.append("Participants: " + ", ".join(people) + ".")
    if any(s.kind == CHAT for s in segments):
        out.append('Lines marked "(chat)" were typed in the meeting chat, not spoken; keep '
                   "links and file names from them exactly as written.")
    return "\n".join(out)
