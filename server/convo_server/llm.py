"""Structured summaries and daily digests via llama-server (OpenAI-compatible API)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import relevance_rules
from .dates import resolve_due, resolve_time
from .store import NOTE_ITEM, Segment, clean_list_name, clean_notes_name

log = logging.getLogger(__name__)


# "note": the user asked a voice note to save a note ("create a note ..."); voice notes only
ItemKind = Literal["task", "event", "reminder", "note"]


class ActionItem(BaseModel):
    text: str = Field(description="What needs to be done, imperative, self-contained")
    kind: ItemKind = Field(default="task",
                           description="event (meeting, appointment, call at a set day), "
                                       "reminder (remind me at a time), note (the user "
                                       "asked to save a note) or task")
    owner: str | None = Field(default=None, description="Who should do it, if stated")
    due: str | None = Field(default=None, description="Deadline as stated, e.g. 'Friday'")
    due_date: str | None = Field(default=None,
                                 description="The deadline as YYYY-MM-DD, if a date can be "
                                             "worked out from the conversation date")
    time: str | None = Field(default=None,
                             description="Time of day as said, e.g. '12 pm', or null")
    duration_minutes: int | None = Field(default=None,
                                         description="How long an event lasts, if said")
    location: str | None = Field(default=None, description="Where an event is, if said")
    list_name: str | None = Field(default=None,
                                  description="The user's list it goes on, if they name one "
                                              "('add milk to my shopping list' -> 'shopping')")
    title: str | None = Field(default=None,
                              description="For a note: a short title, e.g. 'Battery "
                                          "dimensions'; else null")
    at_seconds: int | None = Field(default=None,
                                   description="Offset in seconds where it was mentioned")


class Fact(BaseModel):
    subject: str = Field(description="Person, project or thing the fact is about")
    fact: str = Field(description="Durable fact worth remembering months from now")
    at_seconds: int | None = None


Kind = Literal["meeting", "call", "casual", "appointment", "other"]
DiscardReason = Literal["none", "tv_or_media", "background_noise", "gibberish",
                        "not_a_conversation"]


class Triage(BaseModel):
    """Cheap first call: is this worth summarizing, and what kind of conversation is it?"""
    discard: bool
    reason: DiscardReason
    kind: Kind


class SummaryNotes(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    people: list[str]
    topics: list[str]


class ActionItemList(BaseModel):
    action_items: list[ActionItem]


class FactList(BaseModel):
    facts: list[Fact]


class FactLink(BaseModel):
    new: int = Field(description="Number of the new fact")
    relation: Literal["new", "same", "updates"]
    existing: int | None = Field(default=None, description="id of the known fact it repeats "
                                                           "or updates")


class FactLinks(BaseModel):
    links: list[FactLink]


class ConversationSummary(BaseModel):
    """Everything about one conversation (the shape before the calls were split)."""
    title: str
    summary: str
    key_points: list[str]
    action_items: list[ActionItem]
    people: list[str]
    topics: list[str]
    facts: list[Fact]


class DailyDigest(BaseModel):
    headline: str
    summary: str
    highlights: list[str]
    open_action_items: list[str]
    people: list[str]


COMMON = """Lines look like "[73] Speaker: text" where the number is the offset in seconds \
from the start of the conversation. "{owner}" is the user (the person wearing the \
recorder). Only state what the transcript supports; never invent names, dates or \
commitments. Use the transcript's language for content. Be concise. Output JSON only."""

TRIAGE_SYSTEM = """You sort recordings from a wearable microphone before they are \
summarized. """ + COMMON + """
discard=true only if it is not a real conversation the user took part in or listened to: \
a TV, radio, video or podcast playing (reason tv_or_media), noise or fragments \
(background_noise), unintelligible text (gibberish), or not people talking to each \
other (not_a_conversation). When in doubt, keep it: discard=false, reason "none".
kind: meeting (work discussion with an agenda or several people), call (phone/video \
call), appointment (doctor, dentist, landlord, bank, any service visit), casual (friends, \
family, small talk), other."""

TEMPLATES: dict[str, str] = {
    "meeting": "This is a meeting. summary: purpose and outcome. key_points: decisions "
               "made, open questions, deadlines and who is responsible.",
    "call": "This is a call. summary: who called whom, why, and the outcome. key_points: "
            "what was agreed and any follow-up.",
    "appointment": "This is an appointment. summary: with whom and about what. key_points: "
                   "findings, instructions to follow, the next appointment (date, time, "
                   "place), costs.",
    "casual": "This is a casual conversation. summary: one or two sentences. key_points: "
              "news, plans and personal details worth remembering; skip small talk.",
    "other": "summary: the gist. key_points: the most important points.",
}

SUMMARY_SYSTEM = """You write notes about one conversation for the user's private \
memory. """ + COMMON + """
{template}
title: short and specific (who/what). people: names of people who took part or were \
discussed (not "Speaker 1"); include "{owner}" only if they spoke. topics: a few keywords. \
Keys: title, summary, key_points, people, topics."""

# Both action prompts: what kind of item it is and the time details (resolved in code).
KINDS = """kind: "event" for a meeting, appointment, call or lunch on a set day, e.g. \
"meeting with Jason on Wednesday at 12 pm" -> text "Meet Jason", kind event, due \
"Wednesday", time "12 pm"; "reminder" when the user asks to be reminded at a time, e.g. \
"remind me at 5 to call Mom" -> text "Call Mom", kind reminder, time "5"; otherwise \
"task", e.g. "get the proposal done on Wednesday" -> text "Get the proposal done", kind \
task, due "Wednesday". Something repeating ("every Monday") is a task whose text starts \
"Set up a repeating event: ". time: the time of day as said ("12 pm", "half past two"), \
or null. duration_minutes: how long an event lasts if said ("for an hour" = 60), else \
null. location: where an event is if said ("Main Street"), else null. list_name: the \
list the user wants it on if they name one, e.g. "add milk to my shopping list" -> text \
"Milk", list_name "shopping"; "put call the bank on my work to-do list" -> text "Call the \
bank", list_name "work"; else null."""

ACTION_KEYS = ("Keys: action_items[{{text, kind, owner, due, due_date, time, "
               "duration_minutes, location, list_name, at_seconds}}].")

ACTIONS_SYSTEM = """You list the action items of one conversation. """ + COMMON + """
The conversation happened on {date}.
An action item is something someone promised or was asked to do after this \
conversation: a promise ("I'll send you the slides tonight"), a request ("can you water \
the plants while I'm away?") or an appointment to keep. List every \
one, small ones too; leave out things already done.
text: a task that starts with a verb and names who and what, e.g. "Send Sam the \
slides" or "Water Lena's plants". Never copy the spoken sentence.
owner: who should do it ("{owner}" for the user), if clear. due: the deadline in the \
words used ("Monday", "March 3"), or null; due_date: that deadline as YYYY-MM-DD \
counted from the conversation date, or null. at_seconds: the bracketed number of the line \
it comes from.
""" + KINDS + "\n" + ACTION_KEYS

FACTS_SYSTEM = """You pick durable facts from one conversation for the user's long-term \
memory. """ + COMMON + """
facts: specific facts worth remembering for months: preferences, allergies, plans, \
relationships, jobs, moves, decisions, prices, dates of future events. Skip small talk, \
one-off logistics and anything only true for today. Each fact is a complete sentence that \
names its subject and makes sense on its own, e.g. \
{{"subject": "Alice", "fact": "Alice is vegetarian."}}, never a bare value like "May". \
at_seconds: the bracketed number of the supporting line. Return an empty list if there \
are none. Keys: facts[{{subject, fact, at_seconds}}]."""

LINK_SYSTEM = """You keep the user's memory of facts up to date. You get facts already \
known (with id and the date they were learned) and new facts from a conversation on \
{date}. For every new fact (by its number) say:
- "same": it says the same thing as a known fact (give its id);
- "updates": it is about the same thing as a known fact but the value changed or \
  conflicts, e.g. a new amount, date, place or decision (give its id);
- "new": anything else.
Only link facts about the same subject and the same thing. Output JSON only: \
{{"links": [{{"new": <number>, "relation": "...", "existing": <id or null>}}]}}."""

# Voice notes: the user talking to themselves (an idea, a reminder). Every line is the
# user's; no triage, never discarded.
NOTE_SYSTEM = """You write up a voice note the user recorded for themselves, for their \
private memory. """ + COMMON + """
Every line is the user talking to themselves: an idea, a plan or a reminder.
title: short and specific (what the note is about). summary: the idea in one or two \
sentences. key_points: details worth keeping (may be empty). people: names of people \
mentioned (not "{owner}"). topics: a few keywords. \
Keys: title, summary, key_points, people, topics."""

NOTE_ACTIONS_SYSTEM = """You list the to-dos in a voice note the user recorded for \
themselves. """ + COMMON + """
The note was recorded on {date}.
action_items: things the note says the user wants or needs to do ("call the plumber", \
"remember to buy milk", "I should email Bob"), appointments and meetings it \
mentions, and things to be reminded of. Not vague wishes. text: imperative and \
self-contained ("Call the plumber about the leaking tap"). owner: "{owner}" unless the \
note says someone else should do it. due: the deadline as said ("Friday"); due_date: \
that deadline as YYYY-MM-DD counted from the note's date, or null if there is none. \
at_seconds: the bracketed number of the line it comes from. Return an empty list if \
there are none.
""" + KINDS + """
kind "note" ONLY when the user explicitly asks to save a note: "create a note ...", "make \
a note ...", "take a note ...", "note that ...", "add to my hardware notes ...". text: what \
the note says, cleaned up, without the command words, e.g. "create a note battery \
dimensions are 13mm x 14mm x 3mm" -> text "Battery dimensions are 13 mm x 14 mm x 3 mm", \
title "Battery dimensions". list_name: the notes the user names to add it to, e.g. "add to \
my hardware notes: the screen is 1.4 inch" -> text "The screen is 1.4 inch", title \
"Screen size", list_name "hardware"; else null. A note has no due, time or owner. A plain \
statement without such a command ("the wifi password is on the router") is no item, and \
"remember to ..." is a task, not a note.
""" + ACTION_KEYS.replace("list_name, at_seconds", "list_name, title, at_seconds")

MERGE_SYSTEM = """You merge partial notes of ONE long conversation (given in order) into \
a single set of notes with the same JSON shape. Deduplicate. Output JSON only."""

DIGEST_SYSTEM = """You write a short daily digest of the user's conversations for {date} \
from their structured notes. Only use the given notes. Output JSON only."""


def format_transcript(segments: list[Segment], origin: float) -> str:
    lines = []
    for s in segments:
        off = max(0, int(s.started_at - origin))
        lines.append(f"[{off}] {s.speaker}: {s.text}")
    return "\n".join(lines)


_PLACEHOLDER = re.compile(r"^(speaker\s*\d+|unknown|user|the user)$", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 600.0, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=timeout, transport=transport)
        self.model = model

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   retries: int = 1) -> BaseModel:
        """Chat completion constrained to ``schema`` (llama-server json_schema grammar)."""
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                                "strict": True},
            },
            # Qwen3-style reasoning models: thinking adds latency and breaks JSON-only output
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last: Exception | None = None
        for _ in range(retries + 1):
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as e:  # down, timed out, connection reset
                raise LLMError(f"llama-server unreachable: {type(e).__name__}: {e}") from e
            if resp.status_code >= 400:
                raise LLMError(f"llama-server HTTP {resp.status_code}: {resp.text[:300]}")
            content = resp.json()["choices"][0]["message"].get("content") or ""
            try:
                return schema.model_validate_json(_strip_fences(content))
            except ValidationError as e:
                log.warning("invalid structured output, retrying: %s", e)
                last = e
        raise LLMError(f"model did not return valid {schema.__name__}: {last}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


class Summarizer:
    """Summaries in small focused calls (easier for a small local model than one big one).

    1. triage: discard junk (TV, noise) and pick the conversation kind; plain filler and
       mic checks are discarded first by fixed rules (``relevance_rules``), without a call;
    2. notes: title, summary, key points, people, topics with a template for that kind;
    3. action items, with due dates resolved against the conversation date;
    4. durable facts.
    ``link_facts`` then relates new facts to the ones already known (pipeline).
    """

    def __init__(self, llm: LLMClient, owner_name: str = "Me",
                 max_chars_per_call: int = 120_000, timezone: str = "UTC",
                 min_words: int = 3) -> None:
        self.llm = llm
        self.owner = owner_name
        self.max_chars = max_chars_per_call
        self.tz = ZoneInfo(timezone)
        # fewer words than this in the whole conversation: discarded without a model call
        self.min_words = min_words

    def summarize(self, segments: list[Segment], kind: str | None = None) -> dict[str, Any]:
        """Return a storage-ready dict (absolute ``source_at`` timestamps).

        ``kind`` is the conversation's stored kind; only ``"note"`` (a voice note) changes
        anything: see ``_summarize_note``. Discarded conversations come back as
        ``{"discard": True, "discard_reason", "kind"}`` only.
        """
        if not segments:
            raise ValueError("no segments to summarize")
        origin = segments[0].started_at
        parts = self._split(segments, origin)
        if kind == "note":
            return self._summarize_note(parts, origin)
        if sum(len(s.text.split()) for s in segments) < self.min_words:
            return {"discard": True, "discard_reason": "too_short", "kind": "other"}
        junk = relevance_rules.discard_reason([s.text for s in segments])
        if junk:  # only filler, or a mic check: no model call
            return {"discard": True, "discard_reason": junk, "kind": "other"}
        fmt = {"owner": self.owner}
        triage = self.llm.structured(TRIAGE_SYSTEM.format(**fmt), parts[0], Triage)
        assert isinstance(triage, Triage)
        if triage.discard and triage.reason != "none":
            return {"discard": True, "discard_reason": triage.reason, "kind": triage.kind}
        date = datetime.fromtimestamp(origin, self.tz).strftime("%A %Y-%m-%d")
        notes_sys = SUMMARY_SYSTEM.format(template=TEMPLATES[triage.kind], **fmt)
        actions_sys = ACTIONS_SYSTEM.format(date=date, **fmt)
        facts_sys = FACTS_SYSTEM.format(**fmt)
        notes = [self.llm.structured(notes_sys, p, SummaryNotes) for p in parts]
        if len(notes) > 1:
            merged = self.llm.structured(MERGE_SYSTEM, json.dumps(
                [n.model_dump() for n in notes]), SummaryNotes)
        else:
            merged = notes[0]
        actions = [a for p in parts for a in self.llm.structured(  # type: ignore[attr-defined]
            actions_sys, p, ActionItemList).action_items]
        facts = [f for p in parts for f in self.llm.structured(  # type: ignore[attr-defined]
            facts_sys, p, FactList).facts]
        # notes are voice-note commands only; in a conversation it is a task at most
        actions = [a.model_copy(update={"kind": "task"}) if a.kind == NOTE_ITEM else a
                   for a in actions]
        result = ConversationSummary(**merged.model_dump(), action_items=_unique(actions),
                                     facts=_unique(facts))
        out = to_storage(result, origin)
        out["kind"] = triage.kind
        self._resolve_due_dates(out, origin)
        if not any(s.is_owner for s in segments):  # models tend to add the user anyway
            out["people"] = [p for p in out["people"] if p.lower() != self.owner.lower()]
        return out

    def _summarize_note(self, parts: list[str], origin: float) -> dict[str, Any]:
        """A voice note: no length check and no triage (never discarded), the note
        prompts for notes and to-dos, facts as usual; stored with kind "note"."""
        fmt = {"owner": self.owner}
        date = datetime.fromtimestamp(origin, self.tz).strftime("%A %Y-%m-%d")
        notes_sys = NOTE_SYSTEM.format(**fmt)
        actions_sys = NOTE_ACTIONS_SYSTEM.format(date=date, **fmt)
        facts_sys = FACTS_SYSTEM.format(**fmt)
        notes = [self.llm.structured(notes_sys, p, SummaryNotes) for p in parts]
        merged = notes[0] if len(notes) == 1 else self.llm.structured(
            MERGE_SYSTEM, json.dumps([n.model_dump() for n in notes]), SummaryNotes)
        actions = [a for p in parts for a in self.llm.structured(  # type: ignore[attr-defined]
            actions_sys, p, ActionItemList).action_items]
        facts = [f for p in parts for f in self.llm.structured(  # type: ignore[attr-defined]
            facts_sys, p, FactList).facts]
        out = to_storage(ConversationSummary(**merged.model_dump(), action_items=_unique(actions),
                                             facts=_unique(facts)), origin)
        out["kind"] = "note"
        self._resolve_due_dates(out, origin)
        # only explicit commands make a note: none without the word "note(s)"
        asked = _NOTE_WORD.search("\n".join(parts)) is not None
        out["action_items"] = [i for i in out["action_items"]
                               if i["kind"] != NOTE_ITEM or (asked and i["text"])]
        for item in out["action_items"]:
            if item["kind"] == NOTE_ITEM:  # a note is filed as it is: no date or time
                item.update(due=None, due_date=None, due_time=None, time_unclear=False,
                            owner=None)
        return out

    def _resolve_due_dates(self, out: dict[str, Any], origin: float) -> None:
        """Work the date and time out in code where the phrase names them; the model's
        date guess only for the rest (small models get weekday arithmetic wrong).

        A time with no date ("remind me at 5") is today, or tomorrow when that time had
        already passed when the conversation started."""
        start = datetime.fromtimestamp(origin, self.tz)
        today = start.date()
        for item in out["action_items"]:
            day = resolve_due(item["due"], today) or (
                None if item["due"] else resolve_due(item["text"], today))
            if day is not None:
                item["due_date"] = day.isoformat()
            said = item.pop("time", None)
            if said:
                hhmm, unclear = resolve_time(said)
                unclear = unclear or hhmm is None  # a time was said but can't be read
            else:
                hhmm, unclear = resolve_time(item["due"])
                if hhmm is None and not unclear:
                    hhmm, unclear = resolve_time(item["text"])
            item["due_time"], item["time_unclear"] = hhmm, unclear
            if hhmm and not item["due_date"]:
                at = datetime.strptime(hhmm, "%H:%M").time()
                item["due_date"] = (today if at >= start.time().replace(second=0, microsecond=0)
                                    else today + timedelta(days=1)).isoformat()

    def link_facts(self, date: str, new: list[dict[str, Any]],
                   known: list[dict[str, Any]]) -> list[FactLink]:
        """Relate ``new`` facts (by index) to ``known`` ones (with ``id``)."""
        if not new or not known:
            return []
        user = json.dumps({
            "known": [{"id": k["id"], "learned": k.get("learned"), "subject": k["subject"],
                       "fact": k["fact"]} for k in known],
            "new": [{"number": i, "subject": f["subject"], "fact": f["fact"]}
                    for i, f in enumerate(new)]})
        result = self.llm.structured(LINK_SYSTEM.format(date=date), user, FactLinks)
        by_id = {k["id"]: k for k in known}
        out = []
        for lk in result.links:  # type: ignore[attr-defined]
            if not 0 <= lk.new < len(new):
                continue
            if lk.relation != "new":
                old = by_id.get(lk.existing)  # type: ignore[arg-type]
                # a "same" link drops the new fact, an "updates" link hides the old one:
                # only when the two are visibly about the same thing
                if old is None or not plausible_link(new[lk.new], old):
                    log.info("fact link rejected: %r %s %r", new[lk.new]["fact"],
                             lk.relation, old["fact"] if old else lk.existing)
                    continue
            out.append(lk)
        return out

    def _split(self, segments: list[Segment], origin: float) -> list[str]:
        parts: list[list[Segment]] = [[]]
        size = 0
        for s in segments:
            if size + len(s.text) > self.max_chars and parts[-1]:
                parts.append([])
                size = 0
            parts[-1].append(s)
            size += len(s.text) + 20
        return [format_transcript(p, origin) for p in parts]

    def digest(self, date: str, conversations: list[dict[str, Any]]) -> dict[str, Any]:
        notes = [{
            "time": datetime.fromtimestamp(c["started_at"], UTC).strftime("%H:%M UTC"),
            "title": c["title"], "summary": c["summary"], "key_points": c["key_points"],
            "people": c["people"],
            "action_items": [a["text"] for a in c.get("action_items", [])
                             if a.get("status", "open") == "open"
                             and a.get("kind") != NOTE_ITEM],
            **({"voice_note": True} if c.get("kind") == "note" else {}),
        } for c in conversations]
        result = self.llm.structured(DIGEST_SYSTEM.format(date=date), json.dumps(notes),
                                     DailyDigest)
        out = result.model_dump()
        out["date"] = date
        out["conversation_ids"] = [c["id"] for c in conversations]
        return out


_STOP_WORDS = """a an the and or but of to in on at by for with from about as is are was
were be been being will would shall should can could may might must do does did has have had
not no now this that these those it its it's he she they we you i me my your his her their
our him them us who what when where which there here than then so very just also up out
over into after before until said says say"""
_STOP = frozenset(re.split(r"\s+", _STOP_WORDS))


def _content_words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower()):
        w = w.removesuffix("'s")
        if w in _STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w.rstrip("e") or w)
    return out


def plausible_link(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Two facts can be "the same" or one "update" the other only if they share at
    least two content words besides the subject they have in common (a small model
    otherwise links e.g. "I'll send the spreadsheet" to "avoid hard food", both "Me")."""
    shared_subject = (_content_words(new.get("subject") or "")
                      & _content_words(old.get("subject") or ""))
    common = (_content_words(new["fact"]) & _content_words(old["fact"])) - shared_subject
    return len(common) >= 2


def _unique(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for i in items:
        key = (getattr(i, "text", None) or getattr(i, "fact", "")).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(i)
    return out


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_date(value: str | None) -> str | None:
    """A valid YYYY-MM-DD or None (models sometimes return 'Friday' or '2026-02-30')."""
    if not value or not _DATE.match(value.strip()):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def to_storage(s: ConversationSummary, origin: float) -> dict[str, Any]:
    def at(x: int | None) -> float | None:
        return origin + x if x is not None and x >= 0 else None

    return {
        "title": s.title.strip(),
        "summary": s.summary.strip(),
        "key_points": [k.strip() for k in s.key_points if k.strip()],
        "topics": _dedupe(s.topics),
        "people": [p for p in _dedupe(s.people) if not _PLACEHOLDER.match(p)],
        "action_items": [_item_storage(a, at(a.at_seconds)) for a in s.action_items],
        "facts": [{"subject": f.subject, "fact": f.fact, "source_at": at(f.at_seconds)}
                  for f in s.facts],
    }


_REPEATING = re.compile(r"\b(every|each)\s+(day|week|month|year|morning|evening|night|"
                        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b|"
                        r"\b(daily|weekly|monthly|fortnightly)\b", re.IGNORECASE)
_REPEAT_PREFIX = "Set up a repeating event: "


def _item_storage(a: ActionItem, source_at: float | None) -> dict[str, Any]:
    """One action item for storage; ``time`` (as said) is resolved later and dropped."""
    kind, text = a.kind, a.text.strip()
    # a repeating event can't be filed as one calendar event: a task to set it up
    if kind == "event" and _REPEATING.search(f"{text} {a.due or ''} {a.time or ''}"):
        kind = "task"
        if not text.lower().startswith(_REPEAT_PREFIX.lower()):
            text = _REPEAT_PREFIX + text[:1].lower() + text[1:]
    duration = a.duration_minutes if a.duration_minutes and 0 < a.duration_minutes <= 1440 \
        else None
    if kind == NOTE_ITEM:
        return _note_storage(a, text, source_at)
    text, list_ = _list_and_text(text, a.list_name)
    return {"text": text, "kind": kind, "owner": a.owner, "due": a.due,
            "due_date": _iso_date(a.due_date), "time": a.time,
            "duration_minutes": duration if kind == "event" else None,
            "location": (a.location or "").strip() or None, "list_name": list_,
            "note_title": None, "source_at": source_at}


# "... to my shopping list", "on the work to-do list", "to shopping list": a list the
# user named. "my" / "the" is optional (speech and models drop it), so a name word is
# never one ("add a reminder to check the list" names no list).
_NAME_WORD = r"(?!(?:the|a|an|my|our|your|this|that)\b)[\w'&-]+"
_LIST_PHRASE = re.compile(
    r"\s*\b(?:to|on|onto|in|into|for)\s+(?:(?:my|the|our)\s+)?"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+(?:to-?do\s+|to\s+do\s+)?list\b",
    re.IGNORECASE)
_ADD = re.compile(r"^(?:add|put|write(?:\s+down)?)\s+", re.IGNORECASE)


def _list_and_text(text: str, said: str | None) -> tuple[str, str | None]:
    """The item's text and list. The list is the model's ``list_name``, else an "add X
    to my shopping list" phrase in the text; a phrase naming that list is taken out of
    the text ("Add milk to my shopping list" -> "Milk")."""
    name = clean_list_name(said)
    m = _LIST_PHRASE.search(text)
    said_in_text = clean_list_name(m["name"]) if m else None
    if said_in_text is None:
        return text, name
    if name is None and _ADD.match(text):
        name = said_in_text
    if name is None or name.lower() != said_in_text.lower():
        return text, name
    rest = _ADD.sub("", (text[:m.start()] + text[m.end():]).strip(" ,.")).strip()
    return (rest[:1].upper() + rest[1:] if rest else text), name


# A voice note that asks for a note says the word: "create a note", "note that", "add to
# my hardware notes". Transcribers write "notes" and "note" alike.
_NOTE_WORD = re.compile(r"\bnotes?\b", re.IGNORECASE)
# command words a model may leave in the note's text
_NOTE_COMMAND = re.compile(
    r"^(?:(?:please|ok(?:ay)?|so)[,\s]+)?(?:(?:create|make|take|add|save|write(?:\s+down)?|"
    r"start)\s+(?:a\s+|new\s+|a\s+new\s+)?note|note\s+(?:down\s+)?)"
    r"(?:\s*(?:that|saying|about|of|:|,|-))?\s+", re.IGNORECASE)
# "... to my hardware notes", "in the car notes": notes the user named
_NOTES_PHRASE = re.compile(
    r"\s*\b(?:to|in|into|on|onto)\s+(?:(?:my|the|our)\s+)?"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+notes\b[\s:,.-]*",
    re.IGNORECASE)


def _note_storage(a: ActionItem, text: str, source_at: float | None) -> dict[str, Any]:
    """A note item: its text without the command words, a title, and the notes it goes
    into when the user named them ("add to my hardware notes" -> "hardware")."""
    name = clean_notes_name(a.list_name)
    m = _NOTES_PHRASE.search(text)
    if m and (name is None or name.lower() == (clean_notes_name(m["name"]) or "").lower()):
        name = name or clean_notes_name(m["name"])
        rest = (text[:m.start()] + " " + text[m.end():]).strip(" ,.:-")
        rest = re.sub(r"[\s,]*\b(?:add|put|write)\s+(?:it|this|that)$", "", rest,
                      flags=re.IGNORECASE)
        text = re.sub(r"^(?:add|put|write(?:\s+down)?)\s*[:,-]?\s*", "", rest,
                      flags=re.IGNORECASE) or text
    text = _NOTE_COMMAND.sub("", text.strip()).strip(" :,-") or text.strip()
    text = text[:1].upper() + text[1:]
    title = re.sub(r"\s+", " ", (a.title or "").strip().strip(".\"'"))[:80] or None
    return {"text": text, "kind": NOTE_ITEM, "owner": None, "due": None, "due_date": None,
            "time": None, "duration_minutes": None, "location": None, "list_name": name,
            "note_title": title, "source_at": source_at}


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for i in items:
        k = i.strip()
        if k and k.lower() not in seen:
            seen[k.lower()] = k
    return list(seen.values())
