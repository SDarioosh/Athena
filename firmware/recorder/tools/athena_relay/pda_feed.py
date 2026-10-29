"""The T5 PDA feed (docs/t5-pda-plan.md §4.1), built from the Athena HTTP API.

- `ServerApi` is the small client the PDA needs: `health`, `get`, `patch`, `post`. `HttpApi`
  is the real one (urllib, the same rules as `Uploader`: bearer header only, no redirects,
  every failure an `UploadError` whose `kind` says retry / auth / final).
- `build_feed` makes the feed dict; `encode_feed` the bytes that are PUT (compact UTF-8
  JSON, at most `FEED_MAX`); `content_hash` ignores `version` and `generated`, so an
  unchanged feed isn't pushed again.

Sources: `GET /v1/digests/{today}` or `{yesterday}` (the first that exists),
`GET /v1/conversations?since=<now - 48 h>&limit=20` (summarized ones only),
`GET /v1/action-items?status=open`, the calendar (today to +7 days) through the
`Calendar` protocol of `pda_actions`, the Odysseus checklists (`pda_todos`) and the hub's
Today insight and its rule weights (`pda_insight`). A source that fails for a reason other
than "absent"
(digest 404, calendar not configured, no todos or no insight for this hub/token) fails the
build: the T5 keeps its last feed. The weights never fail it: a failed fetch keeps the
last good table (`last_weights`, kept by the caller).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Protocol

from .pda_insight import MALFORMED
from .uploader import SKIP, Uploader, UploadError

log = logging.getLogger("athena_relay.pda")

FEED_VERSION = 1
FEED_MAX = 32 * 1024  # bytes of encoded JSON (§4.1)
CONV_WINDOW_SECONDS = 48 * 3600
CONV_LIMIT = 20
CAL_DAYS = 7
# Field caps (characters) so one long summary can't crowd out the rest
TITLE_MAX = 120
SUMMARY_MAX = 600
SUMMARY_CUT = 240  # first trimming step when the feed is too big
POINTS_MAX = 5
POINT_MAX = 160
TEXT_MAX = 200
HIGHLIGHTS_MAX = 5
BRIEFING_MAX = 1200
TODO_NOTES_MAX = 20  # §4.4
TODO_ITEMS_MAX = 40
TODO_TEXT_MAX = 160
TEMPLATES_MAX = 12  # event-title templates for the T5's "+" picker (§5)
TEMPLATE_MAX = 40
INSIGHT_HEADLINE_MAX = 200  # the hub's Today insight (§4.1)
INSIGHT_WHY_MAX = 80
INSIGHT_ITEMS_MAX = 5  # per section
INSIGHT_SECTIONS = ("focus", "coming_up", "also")
WEIGHTS_MAX = 32  # entries of the `weights` table (§4.1, §5)
WEIGHT_MIN, WEIGHT_MAX = -6, 6
WEIGHT_ID_MAX = 150  # bytes of a note id in a weight key (the T5's limit)
WEIGHT_PREFIX = "list:"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # fullmatch: ASCII digits only
UTC = timezone.utc  # noqa: UP017 (Python 3.10 has no datetime.UTC)


class ServerApi(Protocol):
    def health(self) -> None: ...

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


class HttpApi(Uploader):
    """`ServerApi` over HTTP. Errors are `UploadError` (RETRY, AUTH or SKIP = final 4xx)."""

    def __repr__(self) -> str:
        return f"HttpApi({self.server!r})"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._request("GET", path + query)

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, body=json.dumps(body).encode(),
                             content_type="application/json")

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, body=json.dumps(body or {}).encode(),
                             content_type="application/json")


def quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


# ------------------------------------------------------------------ time zones


def resolve_tz(name: str) -> tzinfo:
    """An IANA zone name (needs `tzdata` on Windows) or "UTC"."""
    if name.upper() in ("UTC", "Z", "ETC/UTC"):
        return UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {name!r} (on Windows: pip install tzdata)") from e


def tz_offset_minutes(tz: tzinfo, now: float) -> int:
    off = datetime.fromtimestamp(now, tz).utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_z(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ building


def cut(text: Any, limit: int) -> str:
    """`text` as one line of at most `limit` characters, cut at a word with an ellipsis."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    head = s[:limit - 1]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:.") + "…"


def _strings(values: Any, n: int, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [cut(v, limit) for v in values if isinstance(v, str) and v.strip()][:n]


def _briefing(api: ServerApi, today: date) -> dict[str, Any] | None:
    for day in (today, today - timedelta(days=1)):
        try:
            d = api.get(f"/v1/digests/{day.isoformat()}")
        except UploadError as e:
            if e.kind == SKIP and e.status == 404:
                continue
            raise
        text = "\n\n".join(x for x in (cut(d.get("headline"), TITLE_MAX),
                                        cut(d.get("summary"), BRIEFING_MAX)) if x)
        return {"date": d.get("date") or day.isoformat(), "text": text,
                "highlights": _strings(d.get("highlights"), HIGHLIGHTS_MAX, POINT_MAX)}
    return None


def _conversations(api: ServerApi, now: float) -> list[dict[str, Any]]:
    rows = api.get("/v1/conversations", {"since": iso_z(now - CONV_WINDOW_SECONDS),
                                         "limit": CONV_LIMIT}).get("conversations", [])
    out = []
    for c in rows:
        if not isinstance(c, dict) or c.get("status") != "summarized" or not c.get("id"):
            continue  # pending, failed or discarded (e.g. a TV left on)
        out.append({"id": str(c["id"]), "title": cut(c.get("title"), TITLE_MAX),
                    "at": iso_to_z(c.get("started_at")),
                    "people": _strings(c.get("people"), 8, 40),
                    "summary": cut(c.get("summary"), SUMMARY_MAX),
                    "points": _strings(c.get("key_points"), POINTS_MAX, POINT_MAX)})
    out.sort(key=lambda c: c["at"] or "", reverse=True)  # newest first
    return out


def _actions(api: ServerApi) -> list[dict[str, Any]]:
    rows = api.get("/v1/action-items", {"status": "open"}).get("action_items", [])
    out = []
    for a in rows:
        if not isinstance(a, dict) or not isinstance(a.get("id"), int):
            continue
        out.append({"id": a["id"], "text": cut(a.get("text"), TEXT_MAX),
                    "owner": cut(a["owner"], 40) if a.get("owner") else None,
                    "due": a.get("due_date") or None,
                    "conv": a.get("conversation_id")})
    # Due soonest first, undated last; the server's order (newest conversation) breaks ties.
    return sorted(out, key=lambda a: (a["due"] is None, a["due"] or ""))


def _calendar(calendar: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from .pda_actions import CalendarNotConfigured

    if calendar is None:
        return []
    try:
        events = calendar.list_events(start, end)
    except CalendarNotConfigured:
        return []
    out = []
    for ev in events:
        out.append({"id": str(ev["id"]), "title": cut(ev.get("title"), TITLE_MAX),
                    "start": ev.get("start"), "end": ev.get("end"),
                    "where": cut(ev["where"], TEXT_MAX) if ev.get("where") else None,
                    "all_day": bool(ev.get("all_day"))})
    return sorted(out, key=lambda e: e["start"] or "")


def _todos(todos: Any) -> list[dict[str, Any]] | None:
    """The `todos` section (§4.4), or None to leave it out (no Odysseus, or a hub/token that
    can't list notes)."""
    if todos is None:
        return None
    notes = todos.notes()
    if notes is None:
        return None
    out = []
    for n in notes:
        if n.get("type") != "checklist" or not n.get("id"):
            continue
        items = [{"i": k, "text": cut(it.get("text"), TODO_TEXT_MAX), "done": bool(it.get("done"))}
                 for k, it in enumerate((n.get("items") or [])[:TODO_ITEMS_MAX])
                 if isinstance(it, dict)]
        if not items:
            continue
        due = n.get("due_date")
        due = due[:10] if isinstance(due, str) and _DATE.match(due[:10]) else None
        out.append({"id": str(n["id"]), "title": cut(n.get("title"), TITLE_MAX) or None,
                    "pinned": bool(n.get("pinned")), "due": due, "items": items})
        if len(out) == TODO_NOTES_MAX:
            break
    return out


def _feed_refs(feed: dict[str, Any]) -> set[str]:
    """The refs an insight item may point at: `todo:<note id>:<i>` for each item in the
    feed's `todos` and `event:<id>` for each `calendar` entry."""
    refs = {f"todo:{n['id']}:{it['i']}" for n in feed.get("todos") or [] for it in n["items"]}
    refs.update(f"event:{e['id']}" for e in feed.get("calendar") or [])
    return refs


def insight_section(raw: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any] | None:
    """The `insight` section (§4.1) from the hub's answer, or None if the answer is
    malformed (no `YYYY-MM-DD` date or no string headline). Keeps only `date`, `headline`
    and each item's `ref` and `why`, and only refs that resolve in `feed`."""
    day, headline = raw.get("date"), raw.get("headline")
    if not isinstance(day, str) or not _ISO_DATE.fullmatch(day) or not isinstance(headline, str):
        return None
    refs = _feed_refs(feed)
    out: dict[str, Any] = {"date": day, "headline": cut(headline, INSIGHT_HEADLINE_MAX)}
    for key in INSIGHT_SECTIONS:
        rows = raw.get(key)
        items: list[dict[str, str]] = []
        for it in rows if isinstance(rows, list) else []:
            if not isinstance(it, dict) or not isinstance(it.get("ref"), str) \
                    or it["ref"] not in refs:
                continue
            why = it.get("why")
            items.append({"ref": it["ref"],
                          "why": cut(why, INSIGHT_WHY_MAX) if isinstance(why, str) else ""})
            if len(items) == INSIGHT_ITEMS_MAX:
                break
        out[key] = items
    return out


def _insight(insight: Any, feed: dict[str, Any]) -> dict[str, Any] | None:
    """The `insight` section, or None to leave it out (no Odysseus, no insight yet, a token
    without insight:read, or a malformed answer; `pda_insight` logs why)."""
    if insight is None:
        return None
    raw = insight.today()
    if raw is None:
        return None
    out = insight_section(raw, feed)
    if out is None:
        log.warning("Odysseus insight: %s; the feed has no insight", MALFORMED)
    return out


def weights_section(raw: Any) -> dict[str, Any] | None:
    """The `weights` section (§4.1) from the hub's answer, or None if the answer is
    malformed (not an object, `weights` not an object, a `version` other than 1). Keeps only
    `list:<note id>` keys with integer values (not booleans), clamped to [-6, 6], at most 32
    in the hub's order (largest first); everything else is dropped. A valid `date` is kept."""
    if not isinstance(raw, dict) or not isinstance(raw.get("weights"), dict) \
            or raw.get("version", 1) != 1:
        return None
    table: dict[str, int] = {}
    for key, value in raw["weights"].items():
        if len(table) == WEIGHTS_MAX:
            break
        if not isinstance(key, str) or not key.startswith(WEIGHT_PREFIX) \
                or not 0 < len(key[len(WEIGHT_PREFIX):].encode()) <= WEIGHT_ID_MAX:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        table[key] = max(WEIGHT_MIN, min(WEIGHT_MAX, value))
    out: dict[str, Any] = {"version": 1}
    day = raw.get("date")
    if isinstance(day, str) and _ISO_DATE.fullmatch(day):
        out["date"] = day
    out["weights"] = table
    return out


def _weights(insight: Any, last: dict[str, Any] | None) -> dict[str, Any] | None:
    """The hub's weights table, or `last` (the last good one, or None: never fetched) when
    it can't be had now. No Odysseus: None."""
    if insight is None:
        return None
    fetch = getattr(insight, "weights", None)
    raw = fetch() if fetch is not None else None
    if raw is None:
        return last
    out = weights_section(raw)
    if out is None:
        log.warning("Odysseus weights: malformed answer; the feed keeps the last good table")
        return last
    return out


def build_feed(api: ServerApi, *, now: float, tz_name: str, calendar: Any = None,
               templates: list[str] | None = None, todos: Any = None,
               insight: Any = None,
               last_weights: dict[str, Any] | None = None) -> dict[str, Any]:
    """The feed dict (§4.1), not yet trimmed (see `encode_feed`). Raises UploadError.
    `last_weights`: the last good `weights` table (the relay's state); the feed's own
    `weights` is the one to keep next."""
    tz = resolve_tz(tz_name)
    local_now = datetime.fromtimestamp(now, tz)
    today = local_now.date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=tz)
    feed: dict[str, Any] = {"v": FEED_VERSION, "version": int(now), "generated": iso_z(now),
                            "tz": tz_name}
    briefing = _briefing(api, today)
    if briefing is not None:
        feed["briefing"] = briefing
    feed["conversations"] = _conversations(api, now)
    feed["actions"] = _actions(api)
    feed["calendar"] = _calendar(calendar, day_start, day_start + timedelta(days=CAL_DAYS + 1))
    checklists = _todos(todos)
    if checklists is not None:
        feed["todos"] = checklists
    today_insight = _insight(insight, feed)
    if today_insight is not None:
        feed["insight"] = today_insight
    weights = _weights(insight, last_weights)
    if weights is not None:
        feed["weights"] = weights
    if templates:
        feed["templates"] = _strings(templates, TEMPLATES_MAX, TEMPLATE_MAX)
    return feed


# ------------------------------------------------------------------ encoding


def _dump(feed: dict[str, Any]) -> bytes:
    return json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode()


class FeedTooLarge(Exception):
    pass


def encode_feed(feed: dict[str, Any], limit: int = FEED_MAX) -> bytes:
    """Compact JSON of at most `limit` bytes. Trims `feed` in place if it's too big (§4.1):
    summaries are cut first, then the oldest conversations go, then (rarely needed) done
    checklist items, the whole `insight`, the `weights`, the last action items, the last unpinned
    checklists, the last calendar entries and, last of all, the last pinned checklists.
    An `insight` that is kept loses the refs whose item was dropped."""
    raw = _dump(feed)
    if len(raw) <= limit:
        return raw
    convs: list[dict[str, Any]] = feed.get("conversations", [])
    for c in convs:
        c["summary"] = cut(c["summary"], SUMMARY_CUT)
    steps = 0
    while (raw := _dump(feed)) and len(raw) > limit:
        steps += 1
        if convs:
            convs.pop()  # newest first, so this is the oldest
        elif _drop_done_item(feed.get("todos") or []):
            pass
        elif "insight" in feed:
            del feed["insight"]
        elif "weights" in feed:
            del feed["weights"]
        elif feed.get("actions"):
            feed["actions"].pop()
        elif _drop_last(feed.get("todos") or [], lambda n: not n["pinned"]):
            pass
        elif feed.get("calendar"):
            feed["calendar"].pop()
        elif feed.get("todos"):
            feed["todos"].pop()
        else:
            raise FeedTooLarge(f"feed is {len(raw)} bytes with nothing left to drop")
    if "insight" in feed:  # only done items went: drop the refs that pointed at them
        _prune_insight(feed)
        raw = _dump(feed)
    log.info("feed trimmed to %d bytes (summaries cut, %d entries dropped)", len(raw), steps)
    return raw


def _prune_insight(feed: dict[str, Any]) -> None:
    refs = _feed_refs(feed)
    for key in INSIGHT_SECTIONS:
        feed["insight"][key] = [it for it in feed["insight"][key] if it["ref"] in refs]


def _drop_done_item(todos: list[dict[str, Any]]) -> bool:
    """Remove the last done item of the last checklist that has one (a checklist left
    without items goes too). False if there is none."""
    for n in reversed(todos):
        for k in range(len(n["items"]) - 1, -1, -1):
            if n["items"][k]["done"]:
                del n["items"][k]
                if not n["items"]:
                    todos.remove(n)
                return True
    return False


def _drop_last(rows: list[dict[str, Any]], which: Callable[[dict[str, Any]], bool]) -> bool:
    for k in range(len(rows) - 1, -1, -1):
        if which(rows[k]):
            del rows[k]
            return True
    return False


def content_hash(feed: dict[str, Any]) -> str:
    """Hash of the feed without `version` and `generated`, which change on every build."""
    body = {k: v for k, v in feed.items() if k not in ("version", "generated")}
    return hashlib.sha256(_dump(body)).hexdigest()
