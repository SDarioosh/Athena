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
from .store import (
    NOTE_ITEM,
    Segment,
    clean_list_name,
    clean_notes_name,
    is_explicit,
    person_name,
    short_title,
)

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
                              description="A short imperative summary, at most 60 "
                                          "characters, e.g. 'Call the plumber about the "
                                          "tap'; for a note, its title, e.g. 'Battery "
                                          "dimensions'")
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


class ForgetRewrite(BaseModel):
    """A conversation's notes rewritten without facts the user erased (erase_fact)."""
    title: str
    summary: str
    key_points: list[str]
    topics: list[str]


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

ACTION_KEYS = ("Keys: action_items[{{text, title, kind, owner, due, due_date, time, "
               "duration_minutes, location, list_name, at_seconds}}].")

# Both action prompts: what is an action item and what is a fact about the speaker.
ITEM_RULES = """An action item is a concrete thing to do, with an object and ideally a \
time: "send the slides to Greta", "call the plumber on Friday". A statement of intent, \
priority, principle or decision ("I should focus on X before Y", "stop prioritizing Z", \
"we decided to ...", "from now on ...") is NOT an action item: it is a durable fact about \
the speaker, listed with the facts, not here. A vague wish with nothing concrete to act \
on ("figure out how it could use less battery") is not an action item either, unless it \
has a time, a named list or a command word ("remind me", "to-do", "add a task"). \
Something done during the conversation or right after it, that nobody needs reminding \
of, is NOT an action item either: a quick errand or bit of logistics ("I'll move my \
truck", "let me grab my keys", "I'll get the door", "I'm going to park first").
title: a short imperative summary of the item, at most 60 characters, e.g. "Call the \
plumber about the tap"; never the whole sentence."""

ACTIONS_SYSTEM = """You list the action items of one conversation. """ + COMMON + """
The conversation happened on {date}.
An action item is something someone promised or was asked to do after this \
conversation: a promise ("I'll send you the slides tonight"), a request ("can you water \
the plants while I'm away?") or an appointment to keep. List each one the user \
would want to be reminded of later, small promises to someone too ("I'll text you the \
address tonight"); leave out things already done.
""" + ITEM_RULES + """
text: a task that starts with a verb and names who and what, e.g. "Send Sam the \
slides" or "Water Lena's plants". Never copy the spoken sentence, but keep the verb \
the speaker used ("I'll move the car" -> "Move the car", never "Drive the car").
owner: who should do it ("{owner}" for the user), if clear. due: the deadline in the \
words used ("Monday", "March 3"), or null; due_date: that deadline as YYYY-MM-DD \
counted from the conversation date, or null. at_seconds: the bracketed number of the line \
it comes from.
""" + KINDS + "\n" + ACTION_KEYS

FACTS_SYSTEM = """You pick durable facts from one conversation for the user's long-term \
memory. """ + COMMON + """
facts: specific facts worth remembering for months: preferences, allergies, plans, \
relationships, jobs, moves, decisions, prices, dates of future events. Skip small talk, \
one-off logistics and anything only true for today: never how someone felt, slept, ate, \
drank or worked on one day ("was very focused today", "did not drink enough water \
today"), and never a plan for the next minutes or hours or the next step of what they are \
doing ("plans to get a snack", "plans to wire it up on a breadboard to test it"). \
Each fact is a complete sentence that names its subject and makes sense on its own, e.g. \
{{"subject": "Alice", "fact": "Alice is vegetarian."}}, never a bare value like "May". \
at_seconds: the bracketed number of the supporting line. Return an empty list if there \
are none.
Decisions, priorities and principles someone states about how they want to work or live \
are facts about that person (the subject is the speaker, "{owner}" for the user), \
phrased as a decision, e.g. the user saying "I should stop prioritizing side projects \
before the base system is stable" -> {{"subject": "{owner}", "fact": "The owner decided \
to stabilise the base system before side projects."}}. A plan with no time is a fact \
too when it will still matter in a month (a trip, a purchase, a project, a move): "The \
owner plans to ...". Keys: facts[{{subject, fact, at_seconds}}]."""

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
"remember to buy milk", "email Bob about the invoice"), appointments and meetings it \
mentions, and things to be reminded of.
""" + ITEM_RULES + """
text: imperative and \
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
""" + ACTION_KEYS

MERGE_SYSTEM = """You merge partial notes of ONE long conversation (given in order) into \
a single set of notes with the same JSON shape. Deduplicate. Output JSON only."""

FORGET_SYSTEM = """You edit the notes of one conversation because the user asked to \
forget some facts. Remove every statement of the facts to forget, and anything that \
reveals them, from the title, the summary, the key points and the topics. Keep everything \
else word for word. Never add anything. Drop a key point or topic that is only about a \
fact to forget. If the title gives one away, write a neutral title about what else was \
discussed. Output JSON only."""

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

    @property
    def base_url(self) -> str:
        return str(self._http.base_url).rstrip("/")

    def models(self) -> list[str] | None:
        """The model ids the LLM server lists (``GET /models``), or None when it can't say
        (down, or no such route)."""
        try:
            resp = self._http.get("/models", timeout=5.0)
            data = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None
        ids = [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]
        return list(dict.fromkeys(ids))

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
                 min_words: int = 3,
                 digest_skip_lists: str = "Shopping,Groceries,Grocery") -> None:
        self.llm = llm
        self.owner = owner_name
        self.max_chars = max_chars_per_call
        self.tz = ZoneInfo(timezone)
        # fewer words than this in the whole conversation: discarded without a model call
        self.min_words = min_words
        # action items on these lists stay out of the daily digest
        self.digest_skip_lists = {n.lower() for n in map(clean_list_name,
                                  digest_skip_lists.split(",")) if n}

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
        _drop_passing_facts(out)
        self._resolve_due_dates(out, origin)
        _drop_errands(out, _spoken(parts), _lines(parts), origin)
        _mark_explicit(out["action_items"], None)
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
        _drop_passing_facts(out)
        self._resolve_due_dates(out, origin)
        # only explicit commands make a note: none without the word "note(s)"
        asked = _NOTE_WORD.search("\n".join(parts)) is not None
        out["action_items"] = [i for i in out["action_items"]
                               if i["kind"] != NOTE_ITEM or (asked and i["text"])]
        if asked:  # a command the model missed or took for a task
            _notes_from_commands(out["action_items"], parts, origin)
        for item in out["action_items"]:
            if item["kind"] == NOTE_ITEM:  # a note is filed as it is: no date or time
                item.update(due=None, due_date=None, due_time=None, time_unclear=False,
                            owner=None)
        spoken = _spoken(parts)
        _drop_errands(out, spoken, _lines(parts), origin)
        self._intentions_to_facts(out, spoken)
        _mark_explicit(out["action_items"], spoken)
        return out

    def _intentions_to_facts(self, out: dict[str, Any],
                             spoken: list[tuple[str, str]]) -> None:
        """A voice note's decisions and principles ("stop prioritizing side projects",
        "focus on bugs before features") and vague wishes ("figure out how it could use
        less battery") are facts about the owner, not to-dos: small models list them as
        tasks anyway. An undated task without a list or command word that reads like one
        becomes a fact ("The owner decided ..." / "The owner plans ..."), unless the
        facts already say it."""
        items = []
        for item in out["action_items"]:
            fact = self._intention(item, spoken)
            if fact is None:
                items.append(item)
                continue
            want = _content_words(item["text"])
            if not any(want and (got := _content_words(f["fact"]))
                       and len(want & got) >= 0.6 * min(len(want), len(got))
                       for f in out["facts"]):
                out["facts"].append({"subject": self.owner, "fact": fact,
                                     "source_at": item.get("source_at")})
        out["action_items"] = items

    def _intention(self, item: dict[str, Any], spoken: list[tuple[str, str]]) -> str | None:
        """The fact an action item really is (see ``_intentions_to_facts``), or None."""
        if (item["kind"] != "task" or item.get("due_date") or item.get("due_time")
                or item.get("list_name")):
            return None
        owner = (item.get("owner") or "").strip().lower()
        if owner and owner not in {self.owner.lower(), *_OWNER_WORDS}:
            return None  # someone else's to-do
        text = item["text"]
        clause, sentence = _said_in(text, spoken)
        if (_COMMAND_WORDS.search(text) or _COMMAND_WORDS.search(sentence)
                or _after_list_header(text, spoken)):
            return None
        here = (text, clause)
        decided = any(_DECISION.search(x) for x in here) or (
            _INTENT.search(f"{text}\n{sentence}") is not None
            and any(_HABIT.search(x) for x in here))
        if not decided and not any(_VAGUE.search(x) for x in here):
            return None
        return _intent_fact(text, decided)

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
                             and a.get("kind") != NOTE_ITEM
                             and not self._skipped_list(a.get("list_name"))],
            **({"voice_note": True} if c.get("kind") == "note" else {}),
        } for c in conversations]
        result = self.llm.structured(DIGEST_SYSTEM.format(date=date), json.dumps(notes),
                                     DailyDigest)
        out = result.model_dump()
        out["date"] = date
        out["conversation_ids"] = [c["id"] for c in conversations]
        return out

    def remove_facts(self, notes: dict[str, Any], facts: list[str]) -> dict[str, Any]:
        """``notes`` (title, summary, key_points, topics) rewritten without ``facts``.

        The model edits; then code drops any key point or topic, and any summary
        sentence, that still contains a fact's words in order, so a model that ignores the
        instruction can't keep the exact sentence."""
        fields = {"title": notes.get("title") or "", "summary": notes.get("summary") or "",
                  "key_points": list(notes.get("key_points") or []),
                  "topics": list(notes.get("topics") or [])}
        if not facts or not any(fields.values()):
            return fields
        user = json.dumps({"facts_to_forget": facts, "notes": fields}, ensure_ascii=False)
        result = self.llm.structured(FORGET_SYSTEM, user, ForgetRewrite).model_dump()
        return scrub_facts(result, facts)

    def _skipped_list(self, list_name: str | None) -> bool:
        name = clean_list_name(list_name)
        return bool(name) and name.lower() in self.digest_skip_lists


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
        "people": [p for p in _dedupe([person_name(p) for p in s.people])
                   if not _PLACEHOLDER.match(p)],
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
    return {"text": text, "title": short_title(a.title, text), "kind": kind,
            "owner": a.owner, "due": a.due, "due_date": _iso_date(a.due_date),
            "time": a.time, "duration_minutes": duration if kind == "event" else None,
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
    text = re.sub(r"(?<!\.)\.$", "", text[:1].upper() + text[1:])  # a sentence's period
    title = re.sub(r"\s+", " ", (a.title or "").strip().strip(".\"'"))[:80] or None
    return {"text": text, "title": short_title(title, text), "kind": NOTE_ITEM,
            "owner": None, "due": None, "due_date": None, "time": None,
            "duration_minutes": None, "location": None, "list_name": name,
            "note_title": title, "source_at": source_at}


# "[12] Me: text": a line of format_transcript
_LINE = re.compile(r"^\[(?P<at>\d+)\] [^:]*: (?P<text>.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LEAD = re.compile(r"^(?:and|also|then|oh|so)[,\s]+", re.IGNORECASE)
_ADD_TO_NOTES = re.compile(r"^(?:add|put|write(?:\s+down)?)\b", re.IGNORECASE)


def _command_sentences(parts: list[str]) -> list[tuple[str, int]]:
    """Sentences that ask for a note ("create a note ...", "note that ...", "add to my
    hardware notes ..."), with the line's offset in seconds."""
    sentences = [(_LEAD.sub("", sentence.strip()), int(m["at"]))
                 for part in parts for line in part.splitlines()
                 if (m := _LINE.match(line.strip()))
                 for sentence in _SENTENCE_END.split(m["text"]) if sentence.strip()]
    out = []
    i = 0
    while i < len(sentences):
        said, at = sentences[i]
        i += 1
        if _command_only(said) and i < len(sentences):
            # "Create a note. The battery is 13 mm wide.": the note is the next sentence
            said = re.sub(r"^(add|put|write(?:\s+down)?)\s+(?:it|this|that)\b", r"\1",
                          said.rstrip(" .!?:,-"), flags=re.IGNORECASE)
            said = f"{said}: {sentences[i][0]}"
            i += 1
        if _NOTE_COMMAND.match(said + " ") or (
                _ADD_TO_NOTES.match(said) and _NOTES_PHRASE.search(said)):
            out.append((said, at))
    return out


def _command_only(said: str) -> bool:
    """A note command with nothing to note ("Create a note.", "Add to my hardware
    notes."): the transcriber ended the sentence before the note."""
    s = said.strip(" .!?:,-") + " "
    if _ADD_TO_NOTES.match(s) and (m := _NOTES_PHRASE.search(s)):
        rest = (s[:m.start()] + " " + s[m.end():]).strip(" ,.:-")
        return re.fullmatch(r"(?:add|put|write(?:\s+down)?)?(?:\s+(?:it|this|that))?", rest,
                            flags=re.IGNORECASE) is not None
    return _NOTE_COMMAND.match(s) is not None and not _NOTE_COMMAND.sub("", s).strip()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+|\d+", text.lower()))  # "13mm" = "13 mm"


def _covers(item: dict[str, Any], want: set[str]) -> bool:
    """The item says mostly the same words."""
    got = _words(item["text"])
    return bool(got) and len(want & got) >= 0.6 * min(len(want), len(got))


def _notes_from_commands(items: list[dict[str, Any]], parts: list[str],
                         origin: float) -> None:
    """Make sure each spoken note command gives a note: an item the model already wrote
    for it becomes the note (small models take "add to my hardware notes ..." for a
    task); a command without an item gets one, untitled (the hub titles it)."""
    for said, at in _command_sentences(parts):
        note = _note_storage(ActionItem(text=said, kind=NOTE_ITEM), said, origin + at)
        want = _words(note["text"])
        if not want:
            continue
        if any(i["kind"] == NOTE_ITEM and _covers(i, want) for i in items):
            continue
        twin = next((i for i in items if i["kind"] != NOTE_ITEM and not i.get("due_date")
                     and not i.get("due_time") and _covers(i, want)), None)
        if twin is not None:
            twin.update(note, note_title=None, source_at=twin.get("source_at") or origin + at,
                        time_unclear=False)
            twin.pop("time", None)
        else:
            note.pop("time", None)
            note["due_time"], note["time_unclear"] = None, False
            items.append(note)


# Voice notes: how the owner wants to work is a fact, not a to-do (``_intention``).
# A decision or principle: "stop prioritizing side projects", "focus on bugs", "from now
# on ...", "we decided ..."
_DECISION = re.compile(
    r"\b(?:from\s+now\s+on|going\s+forward|decided)\b|\bfocus(?:ing)?\s+on\b"
    r"|\b(?:de)?prioriti[sz](?:e|es|ing)\b|\b(?:stop|quit|avoid)\s+\w+ing\b",
    re.IGNORECASE)
# a vague wish with nothing to act on yet: "figure out how it could use less battery"
_VAGUE = re.compile(r"\b(?:figure|work|find)\s+out\s+(?:how|why|whether|what|if|a\s+way)\b"
                    r"|\bthink\s+about\s+(?:how|why|whether|what|if)\b", re.IGNORECASE)
# intent words: a principle only with a habit or attitude ("I should be more patient",
# "I want to start running"); "I should email Bob" stays a to-do
_INTENT = re.compile(r"\b(?:should|ought\s+to|need\s+to|want\s+to|wanna|going\s+to|"
                     r"gonna|have\s+to|got\s+to|gotta|must)\b", re.IGNORECASE)
_HABIT = re.compile(r"\b(?:be|become|get\s+better\s+at|try\s+(?:to|and))\b"
                    r"|\b(?:start|keep)\s+\w+ing\b|\b(?:more|less)\s+(?:often|time)\b"
                    r"|\b\w+\s+(?:more|less)[.!?]*\s*$", re.IGNORECASE)
# the user asked for a to-do: "remind me", "todo", "to-do", "task:", "add a task", "add
# ... to my ... list", "note that"
_COMMAND_WORDS = re.compile(
    r"\bremind\s+me\b|\breminder\b|\bto-?do\b|\bto\s+do\s+list\b|\btask\s*:"
    r"|\b(?:add|create|make|new)\s+(?:a\s+)?(?:new\s+)?task\b|\bnote\s+that\b"
    r"|\b(?:add|put|write)\b[^.!?]*\blist\b", re.IGNORECASE)
# owners that are the user ("Me" is the summarizer's name for them by default)
_OWNER_WORDS = frozenset({"me", "i", "myself", "user", "the user", "owner", "the owner"})
_CLAUSE_END = re.compile(r";\s*|,?\s+(?:and|but)\s+(?:then\s+|also\s+)?|,\s+(?:then|also)\s+",
                         re.IGNORECASE)
_INTENT_LEAD = re.compile(
    r"^(?:(?:so|okay|ok|and|also|well)[,\s]+)*(?:(?:from\s+now\s+on|going\s+forward)[,\s]+)?"
    r"(?:(?:I|we)(?:'m|\s+am|'re|\s+are)?\s*(?:really\s+|probably\s+|definitely\s+|just\s+)?"
    r"(?:should|ought\s+to|need\s+to|have\s+to|want\s+to|wanna|going\s+to|gonna|got\s+to|"
    r"gotta|must|will|'ll)\s+)?", re.IGNORECASE)
_DECIDED_LEAD = re.compile(r"^(?:I|we)\s+(?:have\s+|'ve\s+|just\s+)?decided\s+(?:to\s+)?",
                           re.IGNORECASE)


def _spoken(parts: list[str]) -> list[tuple[str, str]]:
    """Every clause of the transcript, with its sentence."""
    out = []
    for part in parts:
        for line in part.splitlines():
            m = _LINE.match(line.strip())
            if not m:
                continue
            for sentence in _SENTENCE_END.split(m["text"]):
                sentence = sentence.strip()
                out += [(c.strip(), sentence) for c in _CLAUSE_END.split(sentence)
                        if c.strip()]
    return out


def _said_in(text: str, spoken: list[tuple[str, str]]) -> tuple[str, str]:
    """The clause and sentence an item was written from: the clause with most of the
    item's words; empty strings when none has them."""
    want = _content_words(text)
    best, score = ("", ""), 0.0
    for clause, sentence in spoken:
        got = len(want & _content_words(clause)) / len(want) if want else 0.0
        if got > score:
            best, score = (clause, sentence), got
    return best if score >= 0.6 else ("", "")


# A sentence that starts a to-do list ("Create a to-do list.", "Make a to do list for
# tomorrow.", "To-do list: ..."): the items said after it in the voice note are for it,
# even when the transcriber ended the sentence before them.
_LIST_HEADER = re.compile(
    r"\b(?:create|make|start|begin|write|new)\s+(?:a\s+|my\s+|the\s+)?(?:new\s+)?"
    r"(?:to-?do|to\s+do|task)s?\s+list\b|\b(?:to-?do|to\s+do|task)s?\s+list\s*:",
    re.IGNORECASE)


def _after_list_header(text: str, spoken: list[tuple[str, str]]) -> bool:
    """Was the item said in or after a sentence that starts a to-do list? An item whose
    words aren't found in the note counts too when the note has such a sentence (the
    model reworded it)."""
    header = next((i for i, (_, sentence) in enumerate(spoken)
                   if _LIST_HEADER.search(sentence)), None)
    if header is None:
        return False
    clause, sentence = _said_in(text, spoken)
    if not clause:
        return True
    return any(i >= header for i, x in enumerate(spoken) if x == (clause, sentence))


# Something done right now ("I'll move my truck right now", "I'll grab the keys real
# quick"): nobody needs reminding of it (``_drop_errands``). Not "let me ...": "let me
# check with my wife and get back to you" is a promise for later.
_IMMEDIATE = re.compile(
    r"\b(?:right\s+now|right\s+away|real\s+(?:quick|fast)|in\s+a\s+(?:sec(?:ond)?|minute|"
    r"moment|jiffy)|(?:one|just\s+a)\s+(?:sec(?:ond)?|minute|moment))\b", re.IGNORECASE)


# a fact tied to one day: "was very focused today", "didn't drink enough water today"
_ONE_DAY = re.compile(r"\b(?:today|tonight|yesterday|this\s+(?:morning|afternoon|evening)|"
                      r"right\s+now|just\s+now|at\s+the\s+moment)\b", re.IGNORECASE)


def _drop_passing_facts(out: dict[str, Any]) -> None:
    """Facts are for months; one tied to a single day ("today", "tonight", "this
    morning", "right now") is not one, whatever the model says (small models keep
    them)."""
    keep = []
    for fact in out["facts"]:
        if _ONE_DAY.search(fact["fact"]):
            log.info("dropped a one-day fact: %r", fact["fact"])
            continue
        keep.append(fact)
    out["facts"] = keep


def _lines(parts: list[str]) -> dict[int, str]:
    """The transcript's lines by offset (seconds from the conversation start)."""
    out: dict[int, str] = {}
    for part in parts:
        for line in part.splitlines():
            m = _LINE.match(line.strip())
            if m:
                out.setdefault(int(m["at"]), m["text"])
    return out


def _drop_errands(out: dict[str, Any], spoken: list[tuple[str, str]],
                  lines: dict[int, str], origin: float) -> None:
    """Leave out quick errands done right away ("I'll move my truck right now", "I'll
    grab the keys real quick"): small models list them as to-dos even
    though the prompts say not to. Only an undated task without a list or command word
    whose own clause says it happens now; the clause is found by the item's words, else
    the line it cites (models reword: "move my truck" became "Walk truck to ...")."""
    keep = []
    for item in out["action_items"]:
        if (item["kind"] != "task" or item.get("due_date") or item.get("due_time")
                or item.get("list_name") or _COMMAND_WORDS.search(item["text"])):
            keep.append(item)
            continue
        clause, sentence = _said_in(item["text"], spoken)
        if not clause and item.get("source_at") is not None:
            line = lines.get(int(round(item["source_at"] - origin)), "")
            clauses = [c.strip() for x in _SENTENCE_END.split(line)
                       for c in _CLAUSE_END.split(x.strip()) if c.strip()]
            # only a line of one clause: never guess which of several the item is
            clause, sentence = (clauses[0] if len(clauses) == 1 else ""), line
        if clause and _IMMEDIATE.search(clause) and not _COMMAND_WORDS.search(sentence):
            log.info("dropped a quick errand: %r (%r)", item["text"], clause)
            continue
        keep.append(item)
    out["action_items"] = keep


def _intent_fact(text: str, decided: bool) -> str:
    """"I should stop prioritizing X." -> "The owner decided to stop prioritizing X.";
    "Figure out how ..." -> "The owner plans to figure out how ..."."""
    body = text.strip().rstrip(" .!?")
    body = _DECIDED_LEAD.sub("", _INTENT_LEAD.sub("", body, count=1), count=1) or body
    if body[:1].isupper() and not body[1:2].isupper() and not body.startswith("I "):
        body = body[:1].lower() + body[1:]
    if decided and body.lower().startswith("that "):
        return f"The owner decided {body}."
    return f"The owner {'decided to' if decided else 'plans to'} {body}."


def _mark_explicit(items: list[dict[str, Any]],
                   spoken: list[tuple[str, str]] | None) -> None:
    """``explicit``: the user asked for the item (a date or time, a named list, a
    reminder, event or note, or in a voice note (``spoken``) a command word), so the hub
    files it; the rest are suggestions."""
    for item in items:
        said = ""
        if spoken is not None:
            said = f"{item['text']}\n{_said_in(item['text'], spoken)[1]}"
        item["explicit"] = (is_explicit(item) or _COMMAND_WORDS.search(said) is not None
                            or (spoken is not None and _after_list_header(item["text"], spoken)))


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for i in items:
        k = i.strip()
        if k and k.lower() not in seen:
            seen[k.lower()] = k
    return list(seen.values())


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", (text or "").lower()).split())


def scrub_facts(notes: dict[str, Any], facts: list[str]) -> dict[str, Any]:
    """Drop what still states one of ``facts`` word for word (case and punctuation
    aside): key points and topics that contain it, summary sentences that contain it, a
    title that contains it (becomes "Conversation")."""
    gone = [f for f in (_plain(x) for x in facts) if f]

    def says(text: str) -> bool:
        plain = f" {_plain(text)} "
        return any(f" {f} " in plain for f in gone)

    sentences = re.split(r"(?<=[.!?])\s+", (notes.get("summary") or "").strip())
    return {
        "title": "Conversation" if says(notes.get("title") or "") else notes.get("title") or "",
        "summary": " ".join(x for x in sentences if x and not says(x)),
        "key_points": [k for k in notes.get("key_points") or [] if not says(k)],
        "topics": [t for t in notes.get("topics") or [] if not says(t)],
    }
