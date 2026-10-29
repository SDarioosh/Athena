"""Structured summaries and daily digests via llama-server (``Summarizer``)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import relevance_rules
from ..dates import resolve_due, resolve_time
from ..domain import NOTE_ITEM, Segment, clean_list_name
from .client import LLMClient, LLMError
from .prompts import (
    ACTIONS_SYSTEM,
    DIGEST_SYSTEM,
    FACTS_SYSTEM,
    FORGET_SYSTEM,
    LINK_SYSTEM,
    MERGE_SYSTEM,
    NOTE_ACTIONS_SYSTEM,
    NOTE_SYSTEM,
    REVIEW_SYSTEM,
    SUMMARY_SYSTEM,
    TEMPLATES,
    TRIAGE_SYSTEM,
    format_transcript,
)
from .rules.commands import (
    _ASKED,
    _COMMAND_WORDS,
    _SAID,
    _after_list_header,
    _mark_explicit,
    _notes_from_commands,
    _split_list_lines,
    _todos_from_commands,
)
from .rules.errands import _drop_errands
from .rules.events import _drop_event_facts, _events_from_lines, _name_meetings
from .rules.facts import (
    _DECISION,
    _HABIT,
    _INTENT,
    _OWNER_WORDS,
    _VAGUE,
    _drop_passing_facts,
    _intent_fact,
    _known_matches,
    plausible_link,
    scrub_facts,
)
from .rules.notes import _NOTE_WORD, _lone_note
from .rules.text import _content_words, _lines, _said_in, _said_lines, _spoken
from .schemas import (
    ActionItemList,
    ConversationSummary,
    DailyDigest,
    FactLink,
    FactLinks,
    FactList,
    FactReview,
    ForgetRewrite,
    KnownFacts,
    SummaryNotes,
    Triage,
)
from .storage import to_storage

log = logging.getLogger(__name__)


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
                 digest_skip_lists: str = "Shopping,Groceries,Grocery",
                 fact_min_score: int = 4, max_facts: int = 3) -> None:
        self.llm = llm
        # facts scored under this by the review call are left out (1: no review call)
        self.fact_min_score = fact_min_score
        # at most this many model facts per conversation, best first (0: no cap)
        self.max_facts = max_facts
        self.owner = owner_name
        self.max_chars = max_chars_per_call
        self.tz = ZoneInfo(timezone)
        # fewer words than this in the whole conversation: discarded without a model call
        self.min_words = min_words
        # action items on these lists stay out of the daily digest
        self.digest_skip_lists = {n.lower() for n in map(clean_list_name,
                                  digest_skip_lists.split(",")) if n}

    def summarize(self, segments: list[Segment], kind: str | None = None,
                  known: KnownFacts | None = None,
                  context: str | None = None) -> dict[str, Any]:
        """Return a storage-ready dict (absolute ``source_at`` timestamps).

        ``context`` (optional) is a few lines put before every part of the transcript,
        e.g. a Zoom meeting's topic and participants (``meeting_chat.summary_context``).

        ``known`` (optional) returns the stored facts that may match some candidate facts
        (the pipeline passes the store's): a fact that repeats or updates one of them is
        never cut by the per-conversation cap (``_review_facts``).

        ``kind`` is the conversation's stored kind; only ``"note"`` (a voice note) changes
        anything: see ``_summarize_note``. Discarded conversations come back as
        ``{"discard": True, "discard_reason", "kind"}`` only.
        """
        if not segments:
            raise ValueError("no segments to summarize")
        origin = segments[0].started_at
        parts = self._split(segments, origin)
        if context:  # not a "[n] Name: text" line, so line parsing skips it
            parts = [f"{context}\n{p}" for p in parts]
        if kind == "note":
            return self._summarize_note(parts, origin, known)
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
        facts_sys = FACTS_SYSTEM.format(**fmt) + self._fact_limit()
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
        spoken = _spoken(parts)
        _drop_errands(out, spoken, _lines(parts), origin)
        _name_meetings(out["action_items"], spoken)
        _drop_event_facts(out)
        self._review_facts(out, parts, origin, known)
        _mark_explicit(out["action_items"], None)
        if not any(s.is_owner for s in segments):  # models tend to add the user anyway
            out["people"] = [p for p in out["people"] if p.lower() != self.owner.lower()]
        return out

    def _summarize_note(self, parts: list[str], origin: float,
                        known: KnownFacts | None = None) -> dict[str, Any]:
        """A voice note: no length check and no triage (never discarded), the note
        prompts for notes and to-dos, facts as usual; stored with kind "note"."""
        fmt = {"owner": self.owner}
        date = datetime.fromtimestamp(origin, self.tz).strftime("%A %Y-%m-%d")
        notes_sys = NOTE_SYSTEM.format(**fmt)
        actions_sys = NOTE_ACTIONS_SYSTEM.format(date=date, **fmt)
        facts_sys = FACTS_SYSTEM.format(**fmt) + self._fact_limit()
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
        _todos_from_commands(out["action_items"], parts, origin, self.owner)
        self._resolve_due_dates(out, origin)
        self._command_dates(out, origin)
        # only explicit commands make a note: none without the word "note(s)"
        asked = _NOTE_WORD.search("\n".join(parts)) is not None
        out["action_items"] = [i for i in out["action_items"]
                               if i["kind"] != NOTE_ITEM or (asked and i["text"])]
        if asked:  # a command the model missed or took for a task
            _notes_from_commands(out["action_items"], parts, origin)
        # a meeting said as a fragment ("Meeting with the team Tuesday 11 a.m."): small
        # models often list no item for it, though the summary says it
        _events_from_lines(out, _lines(parts), origin,
                           datetime.fromtimestamp(origin, self.tz).date(), self.owner)
        _lone_note(out)  # after the events: a note said with an event is no lone note
        for item in out["action_items"]:
            if item["kind"] == NOTE_ITEM:  # a note is filed as it is: no date or time
                item.update(due=None, due_date=None, due_time=None, time_unclear=False,
                            owner=None)
        spoken = _spoken(parts)
        _drop_errands(out, spoken, _lines(parts), origin)
        # events first: a fact that only repeats a dated event goes before the review, so
        # it can't take one of the cap's places
        _name_meetings(out["action_items"], spoken)
        _drop_event_facts(out)
        # a decision the owner states in a voice note is kept (not reviewed or capped),
        # whether the model listed it as a fact or as a to-do (``_intentions_to_facts``)
        self._review_facts(out, parts, origin, known, keep_decisions=True)
        self._intentions_to_facts(out, spoken)
        out["action_items"] = _split_list_lines(out["action_items"])
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
                                     "kind": "decision" if fact.startswith(
                                         "The owner decided") else "plan",
                                     "source_at": item.get("source_at")})
        out["action_items"] = items

    def _fact_limit(self) -> str:
        """The facts prompt's cap sentence (``max_facts``)."""
        if not self.max_facts:
            return ""
        # room above the cap: filters and the review drop some, the review keeps the best
        return (f"\nList at most {max(5, 2 * self.max_facts)} facts: the ones the user would "
                "most want recalled months from now.")

    def _review_facts(self, out: dict[str, Any], parts: list[str], origin: float,
                      known: KnownFacts | None = None, keep_decisions: bool = False) -> None:
        """A second look at the model's facts. One call scores each 1-5 (worth recalling
        in three months?, ``REVIEW_SYSTEM``) and facts under ``fact_min_score`` are left
        out; then the best ``max_facts`` are kept (ties: the model's order). A fact the
        review gives no score counts as ``fact_min_score``; a failed review call keeps
        every fact. A fact that repeats or updates a stored one (``known``) is never cut
        by the cap: it adds nothing new, and cutting an update would leave the old value
        current. With ``keep_decisions`` (voice notes), the owner's decisions are neither
        reviewed nor capped."""
        facts = out["facts"]
        if not facts:
            return
        owner = {self.owner.lower(), *_OWNER_WORDS}
        kept = [keep_decisions and f.get("kind") == "decision"
                and (f.get("subject") or "").strip().lower() in owner for f in facts]
        asked_ids = [i for i, k in enumerate(kept) if not k]
        scores: dict[int, int] = {}
        if self.fact_min_score > 1 and asked_ids:
            lines = _said_lines(parts)
            asked = [{"number": i, "subject": facts[i]["subject"], "fact": facts[i]["fact"],
                      "said": lines.get(int(facts[i]["source_at"] - origin), "")
                      if facts[i].get("source_at") is not None else ""}
                     for i in asked_ids]
            try:
                review = self.llm.structured(REVIEW_SYSTEM, json.dumps(
                    {"facts": asked}, ensure_ascii=False), FactReview)
                assert isinstance(review, FactReview)
                scores = {r.number: min(5, max(1, r.score)) for r in review.scores
                          if r.number in asked_ids}
            except LLMError as e:
                log.warning("fact review failed, keeping the facts: %s", e)
        repeats = _known_matches(facts, known) if known is not None else set()
        keep, ranked = [], []
        for i, fact in enumerate(facts):
            if kept[i]:
                keep.append(i)
                continue
            score = scores.get(i, self.fact_min_score)
            if score < self.fact_min_score:
                log.info("dropped a fact the review scored %d: %r", score, fact["fact"])
            elif i in repeats:
                keep.append(i)
            else:
                ranked.append((-score, i))
        ranked.sort()
        if self.max_facts and len(ranked) > self.max_facts:
            for _, i in ranked[self.max_facts:]:
                log.info("dropped a fact over the cap of %d: %r", self.max_facts,
                         facts[i]["fact"])
            ranked = ranked[:self.max_facts]
        keep += [i for _, i in ranked]
        out["facts"] = [facts[i] for i in sorted(keep)]

    def _intention(self, item: dict[str, Any], spoken: list[tuple[str, str]]) -> str | None:
        """The fact an action item really is (see ``_intentions_to_facts``), or None."""
        if (item["kind"] != "task" or item.get("due_date") or item.get("due_time")
                or item.get("list_name") or item.get(_ASKED)):
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

    def _command_dates(self, out: dict[str, Any], origin: float) -> None:
        """The day and time of a to-do said out loud are the ones in its command: small
        models drop them ("make a to-do to get eggs tomorrow" -> "Get eggs", no date;
        "remind me at 5 to call mom" -> no time) and invent a midnight for "remind me to
        call mom" (a to-do due at 00:00 in the hub). Only items a command asked for."""
        start = datetime.fromtimestamp(origin, self.tz)
        for item in out["action_items"]:
            said, remind = item.pop(_SAID, (None, False))
            if said is None or item["kind"] == NOTE_ITEM:
                continue
            day = resolve_due(said, start.date())
            hhmm, unclear = resolve_time(said)
            if day is None and hhmm is None:
                if item.get("due_time") == "00:00" and not re.search(
                        r"\bmidnight\b|\b12\s*a\.?m\b|\b0?0:00\b", said, re.IGNORECASE):
                    log.info("dropped a midnight nobody said: %r", said)
                    item.update(due_time=None, time_unclear=False)
                    if not resolve_due(f"{item.get('due') or ''} {item['text']}", start.date()):
                        item["due_date"] = None
                    if item["kind"] == "reminder":
                        item["kind"] = "task"
                continue
            if item.get("due_date") or item.get("due_time"):
                continue  # the model kept a date: its own
            if hhmm and day is None:
                at = datetime.strptime(hhmm, "%H:%M").time()
                day = start.date() if at >= start.time().replace(second=0, microsecond=0) \
                    else start.date() + timedelta(days=1)
            log.info("dated a spoken to-do the model left undated: %r", said)
            item.update(due_date=day.isoformat() if day else None, due_time=hhmm,
                        time_unclear=unclear)
            if hhmm and item["kind"] == "task" and remind:
                item["kind"] = "reminder"

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
            # a note the owner wrote by hand in the hub, or one they spoke
            **({"written_note": True} if c.get("written") else
               {"voice_note": True} if c.get("kind") == "note" else {}),
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


def _unique(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for i in items:
        key = (getattr(i, "text", None) or getattr(i, "fact", "")).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(i)
    return out
