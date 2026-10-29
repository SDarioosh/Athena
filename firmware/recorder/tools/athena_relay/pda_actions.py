"""Outbox actions from the T5 (docs/t5-pda-plan.md §4.2-§4.3): validate, then call servers.

`parse_action` checks one outbox JSON strictly: a known `t`, exactly the allowed fields, the
right types and ranges. Anything else is `InvalidAction`, which the relay acks with result 1
(§7: raw JSON is never forwarded). `ActionRunner.run` maps a valid action to server calls
and returns an `Outcome`:

- result 0 on 2xx, result 1 on a final 4xx (`UploadError` kind SKIP);
- no ack (result None) on 5xx / network errors (`stop` = retry later with backoff), on
  401/403 (`stop` = auth), or while the calendar isn't configured (the action waits);
- `fetch` returns a detail blob (§4.3) to PUT; it's acked only after `PUT_OK`.

The calendar sits behind the `Calendar` protocol. The Odysseus calendar REST endpoints
aren't confirmed yet (§6), so the only real implementation is `NoCalendar`, which says
"not configured"; tests use a fake.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from .pda_feed import TITLE_MAX, ServerApi, cut, iso_to_z, quote
from .uploader import AUTH, SKIP, UploadError

log = logging.getLogger("athena_relay.pda")

MAX_ACTION_JSON = 400
PAGE_SECONDS = 600  # transcript page (§4.3: 10 minutes)
DETAIL_MAX = 256 * 1024
CAL_TITLE_MAX = 200
CAL_REMEMBER = 200  # cal_add action ids remembered for dedupe

# Outcome.stop
STOP_RETRY = "server_retry"
STOP_AUTH = "auth_failed"

_ID = r"[A-Za-z0-9_.:-]{1,64}"
_NUM = r"\d{1,6}(?:\.\d{1,3})?"
_REF = re.compile(rf"^(conv|transcript):({_ID})(?:@({_NUM})-({_NUM}))?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidAction(Exception):
    """An outbox entry that doesn't parse or doesn't follow §4.2."""


class NotConfigured(Exception):
    """Odysseus (calendar or todos) isn't set up for this relay; the action waits."""


class CalendarNotConfigured(NotConfigured):
    """No calendar is set up on this relay; calendar actions wait in the outbox."""


# ------------------------------------------------------------------ actions


@dataclass(frozen=True)
class ItemStatus:
    id: int
    status: str


@dataclass(frozen=True)
class CalAdd:
    title: str
    start: str  # ISO datetime with offset, or YYYY-MM-DD when all_day
    end: str | None
    all_day: bool
    from_item: int | None = None


@dataclass(frozen=True)
class CalDelete:
    id: str


@dataclass(frozen=True)
class Fetch:
    ref: str
    what: str  # "conv" or "transcript"
    conv_id: str
    start: float | None = None  # transcript page, seconds from the conversation start
    end: float | None = None


@dataclass(frozen=True)
class DigestRefresh:
    date: str


@dataclass(frozen=True)
class TodoToggle:
    note: str
    i: int
    text: str  # the item's first 80 characters, to find it if the indexes shifted
    done: bool


Action = ItemStatus | CalAdd | CalDelete | Fetch | DigestRefresh | TodoToggle

_FIELDS: dict[str, tuple[set[str], set[str]]] = {  # t -> (required, optional)
    "item_status": ({"id", "status"}, set()),
    "cal_add": ({"title", "start"}, {"end", "all_day", "from_item"}),
    "cal_delete": ({"id"}, set()),
    "fetch": ({"ref"}, set()),
    "digest_refresh": ({"date"}, set()),
    "todo_toggle": ({"note", "i", "text", "done"}, set()),
}


def _int_id(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2**63:
        raise InvalidAction(f"{name} must be a positive integer")
    return v


def _str(v: Any, name: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise InvalidAction(f"{name} must be a non-empty string of at most {limit} characters")
    return v


def _date(v: Any, name: str) -> str:
    if not isinstance(v, str) or not _DATE.match(v):
        raise InvalidAction(f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError as e:
        raise InvalidAction(f"{name} is not a real date") from e
    return v


def _datetime(v: Any, name: str) -> datetime:
    if not isinstance(v, str) or len(v) > 40 or "T" not in v:
        raise InvalidAction(f"{name} must be an ISO-8601 date and time")
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidAction(f"{name} must be an ISO-8601 date and time") from e
    if dt.tzinfo is None:
        raise InvalidAction(f"{name} needs a UTC offset or Z")
    return dt


def parse_action(raw: bytes) -> Action:
    if len(raw) > MAX_ACTION_JSON:
        raise InvalidAction(f"{len(raw)} bytes, at most {MAX_ACTION_JSON}")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise InvalidAction("not UTF-8 JSON") from e
    if not isinstance(obj, dict):
        raise InvalidAction("not a JSON object")
    t = obj.pop("t", None)
    if t not in _FIELDS:
        raise InvalidAction(f"unknown type {t!r}")
    required, optional = _FIELDS[t]
    missing, extra = required - obj.keys(), obj.keys() - required - optional
    if missing or extra:
        raise InvalidAction(f"{t}: missing {sorted(missing)}, unknown {sorted(extra)}")

    if t == "item_status":
        if obj["status"] not in ("done", "dismissed", "open"):
            raise InvalidAction("status must be done, dismissed or open")
        return ItemStatus(_int_id(obj["id"], "id"), obj["status"])
    if t == "cal_delete":
        return CalDelete(_str(obj["id"], "id", 200))
    if t == "digest_refresh":
        return DigestRefresh(_date(obj["date"], "date"))
    if t == "todo_toggle":
        i = obj["i"]
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < 10_000:
            raise InvalidAction("i must be an integer >= 0")
        if not isinstance(obj["done"], bool):
            raise InvalidAction("done must be true or false")
        return TodoToggle(_str(obj["note"], "note", 64), i, _str(obj["text"], "text", 80),
                          obj["done"])
    if t == "fetch":
        ref = obj["ref"]
        m = _REF.match(ref) if isinstance(ref, str) else None
        if m is None:
            raise InvalidAction("ref must be conv:<id>, transcript:<id> or "
                                "transcript:<id>@<from>-<to>")
        what, conv_id, a, b = m.groups()
        if a is None:
            return Fetch(ref, what, conv_id)
        if what != "transcript" or float(b) <= float(a):
            raise InvalidAction("a page range needs transcript: and from < to")
        return Fetch(ref, what, conv_id, float(a), float(b))

    # cal_add
    title = _str(obj["title"], "title", CAL_TITLE_MAX)
    all_day = obj.get("all_day", False)
    if not isinstance(all_day, bool):
        raise InvalidAction("all_day must be true or false")
    from_item = _int_id(obj["from_item"], "from_item") if "from_item" in obj else None
    if all_day:
        if "end" in obj:
            raise InvalidAction("an all-day event has no end")
        return CalAdd(title, _date(obj["start"], "start"), None, True, from_item)
    if "end" not in obj:
        raise InvalidAction("cal_add needs end or all_day")
    start, end = _datetime(obj["start"], "start"), _datetime(obj["end"], "end")
    if end <= start:
        raise InvalidAction("end must be after start")
    return CalAdd(title, obj["start"], obj["end"], False, from_item)


# ------------------------------------------------------------------ calendar


class Calendar(Protocol):
    """The Odysseus calendar, as the PDA needs it. Errors are `UploadError` (kinds RETRY,
    AUTH, SKIP) or `CalendarNotConfigured`. Events are dicts with id, title, start, end,
    where, all_day."""

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def create(self, event: CalAdd) -> str:
        """Create the event; return its id."""

    def delete(self, event_id: str) -> None:
        """Delete it; `UploadError` SKIP with status 404 if there is no such event."""


class NoCalendar:
    """The calendar until the Odysseus REST endpoints are confirmed (§6)."""

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise CalendarNotConfigured("calendar not configured")

    def create(self, event: CalAdd) -> str:
        raise CalendarNotConfigured("calendar not configured")

    def delete(self, event_id: str) -> None:
        raise CalendarNotConfigured("calendar not configured")


# ------------------------------------------------------------------ running


@dataclass
class Outcome:
    result: int | None  # 0 done, 1 rejected for good, None = don't ack (stays queued)
    message: str = ""
    detail: dict[str, Any] | None = None  # fetch: PUT this, then ack `result`
    stop: str | None = None  # STOP_RETRY / STOP_AUTH: stop syncing, nothing more acked


@dataclass
class CalendarMemory:
    """cal_add actions already turned into events, so a re-sent action (lost OB_ACKED)
    doesn't create a second event (§4.2). Keyed by action id and the action's JSON, so a T5
    whose ids restart after a wipe isn't mistaken for a repeat."""

    created: dict[str, str] = field(default_factory=dict)
    on_change: Callable[[], None] = lambda: None

    @staticmethod
    def key(action_id: int, raw: bytes) -> str:
        return f"{action_id}:{hashlib.sha256(raw).hexdigest()[:16]}"

    def remember(self, key: str, event_id: str) -> None:
        self.created[key] = event_id
        while len(self.created) > CAL_REMEMBER:
            self.created.pop(next(iter(self.created)))
        self.on_change()


class ActionRunner:
    def __init__(self, api: ServerApi, calendar: Calendar | None = None,
                 memory: CalendarMemory | None = None, todos: Any = None) -> None:
        self.api = api
        self.calendar = calendar or NoCalendar()
        self.memory = memory or CalendarMemory()
        self.todos = todos  # pda_todos.Todos, or None (not configured)
        self._notes: list[dict[str, Any]] | None = None

    def begin_sync(self) -> None:
        """Forget the notes read for todo_toggle: they're read once per sync (§4.4)."""
        self._notes = None

    def run(self, action_id: int, raw: bytes) -> Outcome:
        try:
            action = parse_action(raw)
        except InvalidAction as e:
            return Outcome(1, f"invalid action: {e}")
        try:
            return self._run(action_id, raw, action)
        except NotConfigured as e:
            return Outcome(None, f"{e}; the action waits on the T5")
        except UploadError as e:
            if e.kind == SKIP:
                return Outcome(1, f"rejected by the server: {e}")
            if e.kind == AUTH:
                return Outcome(None, f"token rejected: {e}", stop=STOP_AUTH)
            return Outcome(None, f"server error, retrying later: {e}", stop=STOP_RETRY)

    def _run(self, action_id: int, raw: bytes, a: Action) -> Outcome:
        if isinstance(a, ItemStatus):
            self.api.patch(f"/v1/action-items/{a.id}", {"status": a.status})
            return Outcome(0, f"action item {a.id} -> {a.status}")
        if isinstance(a, DigestRefresh):
            self.api.post(f"/v1/digests/{a.date}")
            return Outcome(0, f"digest {a.date} rebuilt")
        if isinstance(a, Fetch):
            return Outcome(0, f"fetched {a.ref}", detail=self.detail(a))
        if isinstance(a, TodoToggle):
            return self._todo_toggle(a)
        if isinstance(a, CalAdd):
            key = self.memory.key(action_id, raw)
            if key in self.memory.created:
                return Outcome(0, f"event {self.memory.created[key]} already created")
            event_id = self.calendar.create(a)
            self.memory.remember(key, event_id)
            return Outcome(0, f"event {event_id} created")
        assert isinstance(a, CalDelete)
        try:
            self.calendar.delete(a.id)
        except UploadError as e:
            if e.kind == SKIP and e.status == 404:  # already gone, e.g. a re-sent action
                return Outcome(0, f"event {a.id} was already deleted")
            raise
        return Outcome(0, f"event {a.id} deleted")

    # ------------------------------------------------------------ todos (§4.4)

    def _todo_toggle(self, a: TodoToggle) -> Outcome:
        if self.todos is None:
            raise NotConfigured("todos not configured (no Odysseus)")
        if self._notes is None:
            notes = self.todos.notes()
            if notes is None:
                raise NotConfigured("Odysseus can't list todos for this token or hub")
            self._notes = notes
        note = next((n for n in self._notes if str(n.get("id")) == a.note), None)
        if note is None:
            return Outcome(1, f"note {a.note} is gone")
        items = [it if isinstance(it, dict) else {} for it in note.get("items") or []]
        index = match_item(items, a.i, a.text)
        if index is None:
            return Outcome(1, f"no single item of note {a.note} starts with {a.text!r}")
        if bool(items[index].get("done")) == a.done:
            return Outcome(0, f"note {a.note} item {index} already "
                              f"{'done' if a.done else 'open'}")
        try:
            code = self.todos.toggle(a.note, index, a.done)
        except UploadError as e:
            if e.status == 403:  # the token lacks todos:write; fixable, so it waits
                raise NotConfigured("Odysseus token can't change todos (todos:write)") from e
            raise
        if code != 0:
            return Outcome(1, f"Odysseus refused the toggle (exit_code {code})")
        items[index]["done"] = a.done  # later actions in this sync see the new state
        return Outcome(0, f"note {a.note} item {index} -> {'done' if a.done else 'open'}")

    # ------------------------------------------------------------ details (§4.3)

    def detail(self, f: Fetch) -> dict[str, Any]:
        cid = quote(f.conv_id)
        if f.what == "conv":
            c = self.api.get(f"/v1/conversations/{cid}")
            return {"v": 1, "ref": f.ref, "title": cut(c.get("title"), TITLE_MAX),
                    "at": iso_to_z(c.get("started_at")), "people": c.get("people") or [],
                    "summary": c.get("summary") or "", "points": c.get("key_points") or [],
                    "actions": [{"id": i["id"], "text": i.get("text"), "status": i.get("status"),
                                 "due": i.get("due_date")}
                                for i in c.get("action_items") or [] if "id" in i]}
        start = f.start if f.start is not None else 0.0
        end = f.end if f.end is not None else start + PAGE_SECONDS
        params = {"start": start} if start else None
        t = self.api.get(f"/v1/conversations/{cid}/transcript", params)
        segs, more = [], False
        for s in t.get("segments") or []:
            o = float(s.get("offset", 0))
            if o < start:
                continue
            if o >= end:
                more = True
                break
            segs.append({"o": o, "s": s.get("speaker") or "?", "me": bool(s.get("is_owner")),
                         "x": s.get("text") or ""})
        blob = {"v": 1, "ref": f.ref, "title": cut(t.get("title"), TITLE_MAX),
                "from": start, "to": end, "next": None, "segments": segs}
        while len(encode_detail(blob)) > DETAIL_MAX and segs:  # only absurdly long pages
            end = segs.pop()["o"]
            blob["to"], more = end, True
        if more:
            blob["next"] = f"transcript:{f.conv_id}@{_num(end)}-{_num(end + PAGE_SECONDS)}"
        return blob


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def match_item(items: list[dict[str, Any]], i: int, text: str) -> int | None:
    """§4.4: item `i` if its text starts with `text` (spaces trimmed and collapsed), else
    the only item that does, else None."""
    want = _norm(text)
    if 0 <= i < len(items) and _norm(items[i].get("text")).startswith(want):
        return i
    hits = [k for k, it in enumerate(items) if _norm(it.get("text")).startswith(want)]
    return hits[0] if len(hits) == 1 else None


def _num(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def encode_detail(blob: dict[str, Any]) -> bytes:
    return json.dumps(blob, ensure_ascii=False, separators=(",", ":")).encode()
