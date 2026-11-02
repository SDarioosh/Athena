"""Events: what a voice note names with a day and a time, meetings named as said, facts that only
repeat an event."""

from __future__ import annotations

import logging
import re

from ...dates import resolve_due, resolve_time
from ...domain import NOTE_ITEM, short_title
from ..draft import Context, DraftItem, SummaryDraft
from .commands import _COMMAND_WORDS, _split_commands, _starts_command
from .text import _SENTENCE_END, _WHEN_WORDS, _content_words, _meridiem, _overlaps

log = logging.getLogger(__name__)


# A voice note's sentence in the past tense names no event to add (``events_from_lines``),
# nor one that repeats ("yoga every Tuesday at 7 pm": the hub files one-off events only)
_PAST = re.compile(r"\b(?:had|was|were|met|went|did|attended|called|yesterday|ago|last)\b",
                   re.IGNORECASE)
_REPEATS = re.compile(r"\b(?:every|each|weekly|daily|monthly|yearly|weekdays|weekends|"
                      r"(?:mon|tues|wednes|thurs|fri|satur|sun)days)\b", re.IGNORECASE)


def events_from_lines(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A day and a time said in a voice note make a calendar event, unless the sentence
    asks for a to-do, a reminder, a list line or a note ("make a to-do to remind Ola
    about the hike Friday at 3" stays a to-do). Small models list nothing for a fragment
    like "Meeting with the Meshtastic team Tuesday 11 a.m.", or write "Kayaking with
    Ola, Friday at 3" as an undated to-do: the item is added, or its to-do turned into
    the event. Sentences in the past tense or about something that repeats are left
    alone, and so are items on a list.
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
            if (_PAST.search(sentence) or _REPEATS.search(sentence)
                    or _COMMAND_WORDS.search(sentence)):
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

            # the model's item for it, maybe reworded ("Meet Jason" for "Lunch with
            # Jason ..."): the same day from the same line, or most of its words
            twin = next((i for i in items if i.kind != NOTE_ITEM
                         and i.due_date == day.isoformat() and i.source_at == source_at),
                        None) or next((i for i in items if i.kind != NOTE_ITEM
                                       and _overlaps(want, i.text)
                                       and not _said_as_command(i, ctx)), None)
            if twin is None:
                log.info("added an event the model left out: %r", sentence)
                items.append(DraftItem(text=what, title=short_title(None, what),
                                       kind="event", owner=ctx.owner,
                                       due_date=day.isoformat(), due_time=hhmm,
                                       time_unclear=unclear, source_at=source_at))
            elif (twin.kind in ("task", "reminder") and not twin.list_name
                  and twin.due_date in (None, day.isoformat())
                  and twin.due_time in (None, hhmm)
                  and not _COMMAND_WORDS.search(twin.text)):
                # the model wrote it as a to-do: the day and time make it an event
                log.info("made a dated to-do the event it names: %r", sentence)
                twin.kind, twin.due_date = "event", day.isoformat()
                twin.due_time, twin.time_unclear = hhmm, unclear
    return draft


def _said_as_command(item: DraftItem, ctx: Context) -> bool:
    """Is ``item`` what a to-do, reminder, list or note command on its line asked for?
    ("Make a to-do to remind Ola about the hike" is not the hike itself.)"""
    if item.source_at is None:
        return False
    line = ctx.by_offset.get(round(item.source_at - ctx.origin))
    want = _content_words(item.text)
    return line is not None and any(
        (_starts_command(c) or _COMMAND_WORDS.search(c)) and _overlaps(want, c)
        for x in _SENTENCE_END.split(_meridiem(line)) for c in _split_commands(x.strip()))


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
