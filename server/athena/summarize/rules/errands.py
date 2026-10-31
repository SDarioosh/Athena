"""Quick errands done right away ("I'll move my truck right now") are no action items."""

from __future__ import annotations

import logging
import re

from ..draft import Context, SummaryDraft
from .commands import _COMMAND_WORDS
from .text import _CLAUSE_END, _SENTENCE_END, _said_in

log = logging.getLogger(__name__)


# Something done right now ("I'll move my truck right now", "I'll grab the keys real
# quick"): nobody needs reminding of it (``_drop_errands``). Not "let me ...": "let me
# check with my wife and get back to you" is a promise for later.
_IMMEDIATE = re.compile(
    r"\b(?:right\s+now|right\s+away|real\s+(?:quick|fast)|in\s+a\s+(?:sec(?:ond)?|minute|"
    r"moment|jiffy)|(?:one|just\s+a)\s+(?:sec(?:ond)?|minute|moment))\b", re.IGNORECASE)


def drop_errands(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Leave out quick errands done right away ("I'll move my truck right now", "I'll
    grab the keys real quick"): small models list them as to-dos even
    though the prompts say not to. Only an undated task without a list or command word
    whose own clause says it happens now; the clause is found by the item's words, else
    the line it cites (models reword: "move my truck" became "Walk truck to ...")."""
    draft = draft.copy()
    keep = []
    for item in draft.action_items:
        if (item.kind != "task" or item.due_date or item.due_time
                or item.list_name or _COMMAND_WORDS.search(item.text)):
            keep.append(item)
            continue
        clause, sentence = _said_in(item.text, ctx.spoken)
        if not clause and item.source_at is not None:
            line = ctx.by_offset.get(int(round(item.source_at - ctx.origin)), "")
            clauses = [c.strip() for x in _SENTENCE_END.split(line)
                       for c in _CLAUSE_END.split(x.strip()) if c.strip()]
            # only a line of one clause: never guess which of several the item is
            clause, sentence = (clauses[0] if len(clauses) == 1 else ""), line
        if clause and _IMMEDIATE.search(clause) and not _COMMAND_WORDS.search(sentence):
            log.info("dropped a quick errand: %r (%r)", item.text, clause)
            continue
        keep.append(item)
    draft.action_items = keep
    return draft
