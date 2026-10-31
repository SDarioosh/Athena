"""An action item's day and time: worked out in code from what was said."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from ...dates import resolve_due, resolve_time
from ...domain import NOTE_ITEM
from ..draft import Context, SummaryDraft

log = logging.getLogger(__name__)


def resolve_due_dates(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Work the date and time out in code where the phrase names them; the model's
    date guess only for the rest (small models get weekday arithmetic wrong).

    A time with no date ("remind me at 5") is today, or tomorrow when that time had
    already passed when the conversation started. Reads ``DraftItem.said_time`` once."""
    draft = draft.copy()
    start = ctx.start
    today = start.date()
    for item in draft.action_items:
        day = resolve_due(item.due, today) or (
            None if item.due else resolve_due(item.text, today))
        if day is not None:
            item.due_date = day.isoformat()
        said, item.said_time = item.said_time, None
        if said:
            hhmm, unclear = resolve_time(said)
            unclear = unclear or hhmm is None  # a time was said but can't be read
        else:
            hhmm, unclear = resolve_time(item.due)
            if hhmm is None and not unclear:
                hhmm, unclear = resolve_time(item.text)
        item.due_time, item.time_unclear = hhmm, unclear
        if hhmm and not item.due_date:
            at = datetime.strptime(hhmm, "%H:%M").time()
            item.due_date = (today if at >= start.time().replace(second=0, microsecond=0)
                             else today + timedelta(days=1)).isoformat()
    return draft


def command_dates(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """The day and time of a to-do said out loud are the ones in its command: small
    models drop them ("make a to-do to get eggs tomorrow" -> "Get eggs", no date;
    "remind me at 5 to call mom" -> no time) and invent a midnight for "remind me to
    call mom" (a to-do due at 00:00 in the hub). Only items a command asked for
    (``DraftItem.command``, read once)."""
    draft = draft.copy()
    start = ctx.start
    for item in draft.action_items:
        command, item.command = item.command, None
        said, remind = command or (None, False)
        if said is None or item.kind == NOTE_ITEM:
            continue
        day = resolve_due(said, start.date())
        hhmm, unclear = resolve_time(said)
        if day is None and hhmm is None:
            if item.due_time == "00:00" and not re.search(
                    r"\bmidnight\b|\b12\s*a\.?m\b|\b0?0:00\b", said, re.IGNORECASE):
                log.info("dropped a midnight nobody said: %r", said)
                item.due_time, item.time_unclear = None, False
                if not resolve_due(f"{item.due or ''} {item.text}", start.date()):
                    item.due_date = None
                if item.kind == "reminder":
                    item.kind = "task"
            continue
        if item.due_date or item.due_time:
            continue  # the model kept a date: its own
        if hhmm and day is None:
            at = datetime.strptime(hhmm, "%H:%M").time()
            day = start.date() if at >= start.time().replace(second=0, microsecond=0) \
                else start.date() + timedelta(days=1)
        log.info("dated a spoken to-do the model left undated: %r", said)
        item.due_date = day.isoformat() if day else None
        item.due_time, item.time_unclear = hhmm, unclear
        if hhmm and item.kind == "task" and remind:
            item.kind = "reminder"
    return draft
