"""A voice note's decisions, principles and vague wishes are facts about the owner, not
to-dos."""

from __future__ import annotations

from ..draft import Context, DraftItem, FactRow, SummaryDraft
from .commands import _COMMAND_WORDS, _after_list_header
from .facts import _DECISION, _HABIT, _INTENT, _OWNER_WORDS, _VAGUE, _intent_fact
from .text import _content_words, _said_in


def intentions_to_facts(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A voice note's decisions and principles ("stop prioritizing side projects",
    "focus on bugs before features") and vague wishes ("figure out how it could use
    less battery") are facts about the owner, not to-dos: small models list them as
    tasks anyway. An undated task without a list or command word that reads like one
    becomes a fact ("The owner decided ..." / "The owner plans ..."), unless the
    facts already say it."""
    draft = draft.copy()
    items = []
    for item in draft.action_items:
        fact = intention(item, ctx.spoken, ctx.owner)
        if fact is None:
            items.append(item)
            continue
        want = _content_words(item.text)
        if not any(want and (got := _content_words(f["fact"]))
                   and len(want & got) >= 0.6 * min(len(want), len(got))
                   for f in draft.facts):
            draft.facts.append(FactRow(subject=ctx.owner, fact=fact,
                                       kind="decision" if fact.startswith(
                                           "The owner decided") else "plan",
                                       source_at=item.source_at))
    draft.action_items = items
    return draft


def intention(item: DraftItem, spoken: list[tuple[str, str]], owner_name: str) -> str | None:
    """The fact an action item really is (see ``intentions_to_facts``), or None."""
    if (item.kind != "task" or item.due_date or item.due_time
            or item.list_name or item.asked):
        return None
    owner = (item.owner or "").strip().lower()
    if owner and owner not in {owner_name.lower(), *_OWNER_WORDS}:
        return None  # someone else's to-do
    text = item.text
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
