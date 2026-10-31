"""Facts: kinds, passing facts left out, intentions as facts, links to known facts,
and erased facts scrubbed from notes."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from ..draft import Context, SummaryDraft
from ..schemas import FACT_KINDS, KnownFacts
from .text import _content_words

log = logging.getLogger(__name__)


def plausible_link(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Two facts can be "the same" or one "update" the other only if they share at
    least two content words besides the subject they have in common (a small model
    otherwise links e.g. "I'll send the spreadsheet" to "avoid hard food", both "Me")."""
    shared_subject = (_content_words(new.get("subject") or "")
                      & _content_words(old.get("subject") or ""))
    common = (_content_words(new["fact"]) & _content_words(old["fact"])) - shared_subject
    return len(common) >= 2


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


# owners that are the user ("Me" is the summarizer's name for them by default)
_OWNER_WORDS = frozenset({"me", "i", "myself", "user", "the user", "owner", "the owner"})
_INTENT_LEAD = re.compile(
    r"^(?:(?:so|okay|ok|and|also|well)[,\s]+)*(?:(?:from\s+now\s+on|going\s+forward)[,\s]+)?"
    r"(?:(?:I|we)(?:'m|\s+am|'re|\s+are)?\s*(?:really\s+|probably\s+|definitely\s+|just\s+)?"
    r"(?:should|ought\s+to|need\s+to|have\s+to|want\s+to|wanna|going\s+to|gonna|got\s+to|"
    r"gotta|must|will|'ll)\s+)?", re.IGNORECASE)
_DECIDED_LEAD = re.compile(r"^(?:I|we)\s+(?:have\s+|'ve\s+|just\s+)?decided\s+(?:to\s+)?",
                           re.IGNORECASE)


# a fact tied to one day: "was very focused today", "didn't drink enough water today"
_ONE_DAY = re.compile(r"\b(?:today|tonight|yesterday|this\s+(?:morning|afternoon|evening)|"
                      r"right\s+now|just\s+now|at\s+the\s+moment)\b", re.IGNORECASE)


# passing curiosity about something that came up: "is interested in hearing how the
# voice compression sounds", "wants to see what the demo looks like"
_CURIOUS = re.compile(
    r"\b(?:(?:is|was|seems?|seemed)\s+(?:\w+\s+)?(?:interested|curious|keen|eager|excited)"
    r"\s+(?:in|about|to)|wants?\s+to|wanted\s+to)\s+(?:hear(?:ing)?|see(?:ing)?|"
    r"watch(?:ing)?|know(?:ing)?|find(?:ing)?\s+out)\s+(?:how|what|whether|if)\b",
    re.IGNORECASE)


def fact_kind(kind: Any) -> str:
    """A model's fact kind as one of ``FACT_KINDS`` ("other" for anything else)."""
    k = str(kind or "").strip().lower()
    return k if k in FACT_KINDS else "other"


# thinking aloud, not decided: "is considering selling the boat", "might move to Porto",
# "hasn't decided whether to ..."; looked for in the fact's main clause only, so "an
# allergy that might cause anaphylaxis" or "..., though the date might shift" stay
_HEDGE = re.compile(
    r"\b(?:is|are|was|were|has\s+been|have\s+been)\s+(?:(?:\w+ly|still|also|now|just)\s+)*"
    r"(?:considering|contemplating|weighing|wondering|unsure|not\s+sure|undecided|"
    r"debating\s+(?:whether|if|between|\w+ing)\b|thinking\s+(?:about|of)|toying\s+with)\b"
    r"|\b(?:has|have|had)(?:n't|\s+not)\s+(?:yet\s+)?decided\b|\bnever\s+decided\b"
    r"|\b(?:might|maybe|perhaps|possibly)\b", re.IGNORECASE)
# where a fact's main clause ends: a comma or semicolon, or a subordinate clause
_CLAUSE_CUT = re.compile(r"[,;]|\b(?:that|which|who|though|although|because|if|when|"
                         r"unless|since|while|so)\b", re.IGNORECASE)
# a decision settles it, unless negated ("hasn't decided", "not decided", "never decided")
_DECIDED = re.compile(r"(?<!not\s)(?<!n't\s)(?<!never\s)(?<!yet\s)"
                      r"\b(?:decided|has\s+chosen|chose)\b", re.IGNORECASE)
# a request to someone else. Asking or telling someone (a name, a title, a role, a
# pronoun) to do something always is one: "asked Dr. Patel to send ...", "asked the
# landlord not to ...", "Priya asked me to send ...". "Wants X to <verb>" only when X is
# a person of the conversation or a pronoun: "needs German to work in Zurich", "wants
# Friday to stay free" are facts.
_NAME = r"[A-Z][\w'-]*(?:\s+(?:[A-Z][\w'-]*|van|von|de|der|da|del|di|le|la))*"
_TITLE = r"(?:(?:Dr|Mr|Mrs|Ms|Mx|Prof)\.?\s+)"
_ASKED_SOMEONE = re.compile(
    r"\b(?:asked|asks|told|tells|reminded|reminds)\s+"
    r"(?:" + _TITLE + r"?" + _NAME + r"(?:\s+(?:and|&)\s+" + _NAME + r")?"
    r"|me|him|her|them|us|someone|somebody|(?:the|my|our|his|her|their)\s+[a-z]+)"
    r"\s+(?:not\s+)?to\s+[A-Za-z]\w*")
# what "wants Bo to ..." asks of Bo when it is a request (an errand), not a plan such as
# "wants Leo to attend the bilingual school"
_ERRANDS = ("send|write|bring|call|email|e-mail|text|message|fix|check|test|review|share|"
            "book|buy|order|pick|finish|draft|prepare|update|forward|reply|schedule|sign|"
            "pay|look|install|set|clean|return|lend|print|upload|read|ssh|SSH|deliver|"
            "post|file|submit|confirm|organi[sz]e|arrange|handle|sort|grab|get|do|make")
_WANTS = re.compile(
    r"\b(?:wants|wanted|would\s+like|asks\s+for)\s+"
    r"(?P<who>" + _TITLE + r"?" + _NAME + r"(?:\s+(?:and|&)\s+" + _NAME + r")?"
    r"|him|her|them|someone|somebody)\s+(?:not\s+)?to\s+(?:" + _ERRANDS + r")\b")


def _hedged(text: str) -> bool:
    """Thinking aloud in the fact's main clause, not settled by a decision there."""
    main = _CLAUSE_CUT.split(text, maxsplit=1)[0]
    m = _HEDGE.search(main)
    return m is not None and not _DECIDED.search(main)


def _request(text: str, people: Iterable[str]) -> bool:
    """A request to someone else (see ``_ASKED_SOMEONE``, ``_WANTS``)."""
    if _ASKED_SOMEONE.search(text):
        return True
    names = {n.strip().lower() for p in people for n in (p, p.split()[0] if p.split() else "")}
    for m in _WANTS.finditer(text):
        who = m["who"]
        if who.lower() in {"him", "her", "them", "someone", "somebody"}:
            return True
        title = re.match(_TITLE, who)
        bare = who[title.end():] if title else who
        parts = {x.strip().lower() for x in re.split(r"\s+(?:and|&)\s+", bare)}
        if title or any(x in names or x.split()[0] in names for x in parts if x):
            return True
    return False


def drop_passing_facts(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Facts are for months; one tied to a single day ("today", "tonight", "this
    morning", "right now") is not one, and neither is curiosity about something that
    came up ("is interested in hearing how it sounds"), thinking aloud ("is considering
    selling the boat", "might move") or a request to someone else ("wants Bo to send the
    report": an action item), whatever the model says (small models keep them)."""
    draft = draft.copy()
    keep = []
    for fact in draft.facts:
        text = fact["fact"]
        if _ONE_DAY.search(text):
            log.info("dropped a one-day fact: %r", text)
            continue
        if _CURIOUS.search(text):
            log.info("dropped a passing-curiosity fact: %r", text)
            continue
        if _hedged(text):
            log.info("dropped a thinking-aloud fact: %r", text)
            continue
        if _request(text, draft.people or []):
            log.info("dropped a request to someone else: %r", text)
            continue
        keep.append(fact)
    draft.facts = keep
    return draft


def _known_matches(facts: list[dict[str, Any]], known: KnownFacts) -> set[int]:
    """Indexes of ``facts`` that share their subject and a content word (beyond the
    subject's own) with a stored fact: likely the same fact said again, or an update."""
    try:
        stored = known(facts)
    except Exception:  # noqa: BLE001  (a lookup problem must not fail the summary)
        log.exception("known facts lookup failed; capping all facts")
        return set()
    out = set()
    for i, f in enumerate(facts):
        subj = (f.get("subject") or "").strip().lower()
        words = _content_words(f["fact"]) - _content_words(subj)
        for k in stored:
            if (k.get("subject") or "").strip().lower() == subj and \
                    words & (_content_words(k.get("fact") or "") - _content_words(subj)):
                out.add(i)
                break
    return out


def review_facts(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A second look at the model's facts (``_review``)."""
    return _review(draft, ctx, keep_decisions=False)


def review_facts_keep_decisions(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """``review_facts`` for a voice note: the owner's decisions are kept, neither
    reviewed nor capped."""
    return _review(draft, ctx, keep_decisions=True)


def _review(draft: SummaryDraft, ctx: Context, keep_decisions: bool) -> SummaryDraft:
    """A second look at the model's facts. One call scores each 1-5 (worth recalling
    in three months?, ``REVIEW_SYSTEM``, ``Context.review``) and facts under
    ``fact_min_score`` are left out; then the best ``max_facts`` are kept (ties: the
    model's order). A fact the review gives no score counts as ``fact_min_score``; a
    failed review call keeps every fact. A fact that repeats or updates a stored one
    (``known``) is never cut by the cap: it adds nothing new, and cutting an update would
    leave the old value current. With ``keep_decisions`` (voice notes), the owner's
    decisions are neither reviewed nor capped."""
    draft = draft.copy()
    facts = draft.facts
    if not facts:
        return draft
    owner = {ctx.owner.lower(), *_OWNER_WORDS}
    kept = [keep_decisions and f.get("kind") == "decision"
            and (f.get("subject") or "").strip().lower() in owner for f in facts]
    asked_ids = [i for i, k in enumerate(kept) if not k]
    scores: dict[int, int] = {}
    if ctx.fact_min_score > 1 and asked_ids and ctx.review is not None:
        lines = ctx.said_by_offset
        asked = [{"number": i, "subject": facts[i]["subject"], "fact": facts[i]["fact"],
                  "said": lines.get(int(facts[i]["source_at"] - ctx.origin), "")
                  if facts[i].get("source_at") is not None else ""}
                 for i in asked_ids]
        scores = {number: min(5, max(1, score)) for number, score in ctx.review(asked)
                  if number in asked_ids}
    repeats = _known_matches(facts, ctx.known) if ctx.known is not None else set()
    keep, ranked = [], []
    for i, fact in enumerate(facts):
        if kept[i]:
            keep.append(i)
            continue
        score = scores.get(i, ctx.fact_min_score)
        if score < ctx.fact_min_score:
            log.info("dropped a fact the review scored %d: %r", score, fact["fact"])
        elif i in repeats:
            keep.append(i)
        else:
            ranked.append((-score, i))
    ranked.sort()
    if ctx.max_facts and len(ranked) > ctx.max_facts:
        for _, i in ranked[ctx.max_facts:]:
            log.info("dropped a fact over the cap of %d: %r", ctx.max_facts,
                     facts[i]["fact"])
        ranked = ranked[:ctx.max_facts]
    keep += [i for _, i in ranked]
    draft.facts = [facts[i] for i in sorted(keep)]
    return draft


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
