"""Structured summaries and daily digests via llama-server (``Summarizer``)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .. import relevance_rules
from ..domain import NOTE_ITEM, Segment, clean_list_name
from .client import LLMClient, LLMError
from .draft import Context, SummaryDraft
from .passes import CONVERSATION_PASSES, NOTE_PASSES, run_passes
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
from .rules.facts import plausible_link, scrub_facts
from .rules.text import transcript_lines
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
    Then fixed rules work on the answers, in the order of ``passes.NOTE_PASSES`` (a
    voice note) or ``passes.CONVERSATION_PASSES``. ``link_facts`` then relates new facts
    to the ones already known (pipeline).
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
        e.g. a Zoom meeting's topic and participants (``meetings.ingest.chat.summary_context``).

        ``known`` (optional) returns the stored facts that may match some candidate facts
        (the pipeline passes the store's): a fact that repeats or updates one of them is
        never cut by the per-conversation cap (``rules.facts.review_facts``).

        ``kind`` is the conversation's stored kind; only ``"note"`` (a voice note) changes
        anything: see ``_summarize_note``. Discarded conversations come back as
        ``{"discard": True, "discard_reason", "kind"}`` only.
        """
        if not segments:
            raise ValueError("no segments to summarize")
        origin = segments[0].started_at
        parts = self._split(segments, origin)
        if context:  # for the model; the rules read the segments (``transcript_lines``)
            parts = [f"{context}\n{p}" for p in parts]
        if kind == "note":
            return self._summarize_note(segments, parts, origin, known, context)
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
        draft = SummaryDraft.from_storage(to_storage(result, origin), triage.kind)
        ctx = self._context(segments, origin, known, context)
        return run_passes(draft, ctx, CONVERSATION_PASSES).storage()

    def _context(self, segments: list[Segment], origin: float, known: KnownFacts | None,
                 context: str | None) -> Context:
        """What the rules read besides the summary (``passes.py``)."""
        return Context(segments=segments, lines=transcript_lines(segments, origin),
                       origin=origin, tz=self.tz, owner=self.owner, context=context,
                       known=known, review=self._review, fact_min_score=self.fact_min_score,
                       max_facts=self.max_facts)

    def _review(self, asked: list[dict[str, Any]]) -> list[tuple[int, int]]:
        """The fact review call (``REVIEW_SYSTEM``): (number, score) as the model gave
        them; none when the call fails (every fact is kept)."""
        try:
            review = self.llm.structured(REVIEW_SYSTEM, json.dumps(
                {"facts": asked}, ensure_ascii=False), FactReview)
            assert isinstance(review, FactReview)
            return [(r.number, r.score) for r in review.scores]
        except LLMError as e:
            log.warning("fact review failed, keeping the facts: %s", e)
            return []

    def _summarize_note(self, segments: list[Segment], parts: list[str], origin: float,
                        known: KnownFacts | None = None,
                        context: str | None = None) -> dict[str, Any]:
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
        draft = SummaryDraft.from_storage(to_storage(ConversationSummary(
            **merged.model_dump(), action_items=_unique(actions), facts=_unique(facts)),
            origin), "note")
        ctx = self._context(segments, origin, known, context)
        return run_passes(draft, ctx, NOTE_PASSES).storage()

    def _fact_limit(self) -> str:
        """The facts prompt's cap sentence (``max_facts``)."""
        if not self.max_facts:
            return ""
        # room above the cap: filters and the review drop some, the review keeps the best
        return (f"\nList at most {max(5, 2 * self.max_facts)} facts: the ones the user would "
                "most want recalled months from now.")

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
