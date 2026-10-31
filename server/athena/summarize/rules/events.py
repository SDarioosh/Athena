"""Events: appointments a voice note names, meetings named as said, facts that only
repeat an event."""

from __future__ import annotations

import logging
import re

from ...dates import resolve_due, resolve_time
from ...domain import NOTE_ITEM, short_title
from ..draft import Context, DraftItem, SummaryDraft
from .commands import _split_commands, _starts_command
from .text import _SENTENCE_END, _WHEN_WORDS, _content_words, _meridiem, _overlaps

log = logging.getLogger(__name__)


# A voice note's appointment the model left out (``_events_from_lines``): a sentence
# with an appointment word, a day and a time, not in the past tense.
_APPOINTMENT = re.compile(
    r"\b(?:meet(?:ing)?s?|appointments?|appt|calls?|lunch|dinner|breakfast|brunch|coffee|"
    r"interviews?|dentist|doctor|doctor's|checkup|check-up|class|lesson|session|party|"
    r"standup|stand-up|sync|demo|presentation|webinar|reservation)\b", re.IGNORECASE)
_PAST = re.compile(r"\b(?:had|was|were|met|went|did|attended|called|yesterday|ago|last)\b",
                   re.IGNORECASE)


def events_from_lines(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Make sure each appointment a voice note names with a day and a time is an event.
    Small models list nothing for a fragment like "Meeting with the Meshtastic team
    Tuesday 11 a.m." (no verb, no command word), so it reached neither the calendar nor
    the suggestions. Only a sentence with an appointment word, a day and a time, not in
    the past tense, that no item already covers (most of its words, or the same day
    from the same line). An undated to-do with most of its words gets its day and time.
    """
    draft = draft.copy()
    items, origin, today = draft.action_items, ctx.origin, ctx.today
    for at, line in sorted(ctx.by_offset.items()):
        # a to-do or note command said with it ("Meeting with Jason on Wednesday at 12 pm,
        # make a note to bring the slides") is its own item, never the event's name
        for sentence in (c for x in _SENTENCE_END.split(_meridiem(line))
                         for c in _split_commands(x.strip())
                         if not _starts_command(c)):
            sentence = sentence.strip()
            if not _APPOINTMENT.search(sentence) or _PAST.search(sentence):
                continue
            day = resolve_due(sentence, today)
            hhmm, unclear = resolve_time(sentence)
            if day is None or hhmm is None:
                continue
            what = _WHEN_WORDS.sub(" ", sentence)
            what = re.sub(r"\s+", " ", what).strip(" ,.;:-!?") or sentence.rstrip(".")
            what = what[:1].upper() + what[1:]
            want = _content_words(what)
            source_at = origin + at

            # the model's item for it, reworded ("Meet Jason" for "Lunch with Jason ...")
            if any(i.due_date == day.isoformat() and i.source_at == source_at
                   for i in items):
                continue
            twin = next((i for i in items if i.kind != NOTE_ITEM
                         and _overlaps(want, i.text)), None)
            if twin is not None:  # the model wrote it as an undated to-do: date it
                if (twin.kind in ("task", "reminder") and not twin.due_date
                        and not twin.due_time and not twin.list_name):
                    log.info("dated an appointment the model left undated: %r", sentence)
                    twin.kind, twin.due_date = "event", day.isoformat()
                    twin.due_time, twin.time_unclear = hhmm, unclear
                continue
            log.info("added an event the model left out: %r", sentence)
            items.append(DraftItem(text=what, title=short_title(None, what), kind="event",
                                   owner=ctx.owner, due_date=day.isoformat(), due_time=hhmm,
                                   time_unclear=unclear, source_at=source_at))
    return draft


# "Meet the Meshtastic team" for "Meeting with Meshtastic team ...": the meeting's name
# as said (``_name_meetings``)
_MEET = re.compile(r"^meet(?:\s+(?:up\s+)?with)?\s+(?P<who>.+)$", re.IGNORECASE)
_MEETING_WITH = re.compile(r"\bmeeting\s+with\s+(?P<who>.+)$", re.IGNORECASE)


def name_meetings(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """An event the model wrote as "Meet X" when the user said "meeting with X" is
    named as said: "Meet the Meshtastic team" means getting to know them, the calendar
    should say "Meeting with Meshtastic team". The day and time are left out."""
    draft = draft.copy()
    for item in draft.action_items:
        m = _MEET.match(item.text.strip())
        if item.kind != "event" or m is None:
            continue
        said = next((c for c, _ in ctx.spoken if (w := _MEETING_WITH.search(_meridiem(c)))
                     and _overlaps(_content_words(m["who"]), w["who"])), None)
        if said is None:
            continue
        who = _MEETING_WITH.search(_meridiem(said))["who"]  # type: ignore[index]
        who = re.sub(r"\s+", " ", _WHEN_WORDS.sub(" ", who)).strip(" ,.;:-!?")
        if not who:
            continue
        name = f"Meeting with {who}"
        if not item.title or _MEET.match(item.title.strip()):
            item.title = short_title(None, name)
        item.text = name
    return draft


def drop_event_facts(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A fact that only repeats a dated event of the same conversation with its weekday
    or time ("Shahyah has a meeting with the Meshtastic team on Tuesday at 11 a.m.") is
    left out: the event is on the calendar, and the fact would be wrong a week later.
    A fact that names no weekday or time ("Alice's wedding is on June 14") stays."""
    draft = draft.copy()
    events = [_content_words(i.text) for i in draft.action_items
              if i.kind == "event" and i.due_date]
    keep = []
    for fact in draft.facts:
        text = fact["fact"]
        if _WHEN_WORDS.search(_meridiem(text)) and any(
                _overlaps(want, text) for want in events):
            log.info("dropped a fact that repeats an event: %r", text)
            continue
        keep.append(fact)
    draft.facts = keep
    return draft
