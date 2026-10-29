"""The rules that run on the model's summary, in order: one list per kind of recording.

Each pass is a function ``(SummaryDraft, Context) -> SummaryDraft`` that returns a
changed copy (``draft.py``). The order matters; the comments say why where it does."""

from __future__ import annotations

from collections.abc import Sequence

from .draft import Context, Pass, SummaryDraft
from .rules.commands import (
    mark_explicit,
    mark_explicit_voice_note,
    notes_from_commands,
    split_list_lines,
    todos_from_commands,
)
from .rules.dates import command_dates, resolve_due_dates
from .rules.errands import drop_errands
from .rules.events import drop_event_facts, events_from_lines, name_meetings
from .rules.facts import (
    drop_passing_facts,
    review_facts,
    review_facts_keep_decisions,
)
from .rules.intentions import intentions_to_facts
from .rules.notes import keep_asked_notes, lone_note, undate_notes


def drop_silent_owner(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A conversation the owner didn't speak in doesn't list them among its people:
    models tend to add the user anyway."""
    if any(s.is_owner for s in ctx.segments):
        return draft
    draft = draft.copy()
    draft.people = [p for p in draft.people if p.lower() != ctx.owner.lower()]
    return draft


# A voice note (conversation kind "note").
NOTE_PASSES: tuple[Pass, ...] = (
    drop_passing_facts,
    # before the dates: a to-do a command asked for brings its command's day and time
    todos_from_commands,
    resolve_due_dates,
    command_dates,
    keep_asked_notes,
    notes_from_commands,
    # a meeting said as a fragment ("Meeting with the team Tuesday 11 a.m."): small
    # models often list no item for it, though the summary says it
    events_from_lines,
    lone_note,  # after the events: a note said with an event is no lone note
    undate_notes,
    drop_errands,
    # events first: a fact that only repeats a dated event goes before the review, so
    # it can't take one of the cap's places
    name_meetings,
    drop_event_facts,
    # a decision the owner states in a voice note is kept (not reviewed or capped),
    # whether the model listed it as a fact or as a to-do (``intentions_to_facts``)
    review_facts_keep_decisions,
    intentions_to_facts,
    split_list_lines,
    mark_explicit_voice_note,
)

# A conversation (any other kind).
CONVERSATION_PASSES: tuple[Pass, ...] = (
    drop_passing_facts,
    resolve_due_dates,
    drop_errands,
    name_meetings,
    drop_event_facts,
    review_facts,
    mark_explicit,
    drop_silent_owner,
)


def run_passes(draft: SummaryDraft, ctx: Context, passes: Sequence[Pass]) -> SummaryDraft:
    for step in passes:
        draft = step(draft, ctx)
    return draft
