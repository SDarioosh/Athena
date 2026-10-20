"""Structured summaries and daily digests via llama-server (OpenAI-compatible API)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import relevance_rules
from .dates import resolve_due
from .store import Segment

log = logging.getLogger(__name__)


class ActionItem(BaseModel):
    text: str = Field(description="What needs to be done, imperative, self-contained")
    owner: str | None = Field(default=None, description="Who should do it, if stated")
    due: str | None = Field(default=None, description="Deadline as stated, e.g. 'Friday'")
    due_date: str | None = Field(default=None,
                                 description="The deadline as YYYY-MM-DD, if a date can be "
                                             "worked out from the conversation date")
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
it comes from. Keys: action_items[{{text, owner, due, due_date, at_seconds}}]."""

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
"remember to buy milk", "I should email Bob"). Not vague wishes. text: imperative and \
self-contained ("Call the plumber about the leaking tap"). owner: "{owner}" unless the \
note says someone else should do it. due: the deadline as said ("Friday"); due_date: \
that deadline as YYYY-MM-DD counted from the note's date, or null if there is none. \
at_seconds: the bracketed number of the line it comes from. Return an empty list if \
there are none. Keys: action_items[{{text, owner, due, due_date, at_seconds}}]."""

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
        return out

    def _resolve_due_dates(self, out: dict[str, Any], origin: float) -> None:
        """Work the date out in code where the phrase names a day; the model's guess only
        for the rest (small models get weekday arithmetic wrong)."""
        today = datetime.fromtimestamp(origin, self.tz).date()
        for item in out["action_items"]:
            day = resolve_due(item["due"], today) or (
                None if item["due"] else resolve_due(item["text"], today))
            if day is not None:
                item["due_date"] = day.isoformat()

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
                             if a.get("status", "open") == "open"],
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
        "action_items": [{"text": a.text, "owner": a.owner, "due": a.due,
                          "due_date": _iso_date(a.due_date), "source_at": at(a.at_seconds)}
                         for a in s.action_items],
        "facts": [{"subject": f.subject, "fact": f.fact, "source_at": at(f.at_seconds)}
                  for f in s.facts],
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for i in items:
        k = i.strip()
        if k and k.lower() not in seen:
            seen[k.lower()] = k
    return list(seen.values())
