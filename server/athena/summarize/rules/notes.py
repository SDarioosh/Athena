"""Note items: a voice note's "create a note ..." command, its text, title and notes."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from ...domain import NOTE_ITEM, clean_notes_name, short_title
from ..draft import Context, SummaryDraft
from ..schemas import ActionItem
from .text import _NAME_WORD

# A voice note that asks for a note says the word: "create a note", "note that", "add to
# my hardware notes". Transcribers write "notes" and "note" alike.
_NOTE_WORD = re.compile(r"\bnotes?\b", re.IGNORECASE)
# command words a model may leave in the note's text: "make a note", "new note:", "note:",
# "note to self,"
_NOTE_COMMAND = re.compile(
    r"^(?:(?:please|ok(?:ay)?|so)[,\s]+)?(?:(?:create|make|take|add|save|write(?:\s+down)?|"
    r"start|new)\s+(?:a\s+|new\s+|a\s+new\s+)?note(?:\s+to\s+(?:self|myself))?"
    r"|note(?:\s+to\s+(?:self|myself))?(?:\s+down)?(?=[\s:,-])"
    r"|(?:add|put|write(?:\s+down)?)\s+(?:(?:this|it)\s+)?(?:to|in|into)\s+(?:my|the)\s+notes?)"
    r"(?:\s*(?:that|saying|about|of|:|,|-))?\s+", re.IGNORECASE)
# "... to my hardware notes", "in the car notes", "to my car note": notes the user named
# (one "note" only after my / the / our: "add a note to buy eggs" names none)
_NOTES_PHRASE = re.compile(
    r"\s*\b(?:to|in|into|on|onto)\s+(?:(?P<det>my|the|our)\s+)?"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+(?:notes|(?(det)note|(?!)))\b"
    r"[\s:,.-]*", re.IGNORECASE)
# "make a note in voice recorder that ...", "make a note under car, ...", "make a note for
# the voice recorder: ...": the notes named without the word "notes", read only right
# after a note command (``_note_place``)
_NOTE_PLACE = re.compile(
    r"^(?P<prep>in|into|on|onto|under|for|to)\s+(?:(?P<det>my|the|our)\s+)?"
    r"(?P<name>[\w'&-]+(?:\s+[\w'&-]+){0,3}?)\s*(?:[,:;]|\s[-–—]|\s+that\b|\s+saying\b)\s*"
    r"(?P<rest>\S.*)$", re.IGNORECASE)
# what such a phrase names when it is no notes: a time, a person, "case" ("in case ...")
_NOT_A_PLACE = re.compile(
    r"^(?:self|myself|me|you|us|him|her|them|later|now|today|tonight|tomorrow|case|general"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening"
    r"|(?:this|next|last)\s+\w+|(?:a|an|the)?\s*(?:minute|second|moment|hour|bit|while))\b"
    r"|\b(?:how|what|why|when|where|if|whether|which|who|a|an|that|this|is|are|was)\b",
    re.IGNORECASE)


def _note_place(text: str) -> tuple[str, str] | None:
    """(notes name, the note) for "in voice recorder that the case needs ..." said right
    after a note command; None when the words name no notes ("in case I forget, ...",
    "for later: ...", "to call Mom, ..."). "for" and "to" need my / the / our."""
    m = _NOTE_PLACE.match(text.strip())
    if m is None or (m["prep"].lower() in ("for", "to") and not m["det"]):
        return None
    name = m["name"].strip()
    if _NOT_A_PLACE.search(name) or (m["prep"].lower() == "in" and not m["det"]
                                      and name.lower().startswith("case")):
        return None
    clean = clean_notes_name(name)
    rest = m["rest"].strip()
    return (clean, rest[:1].upper() + rest[1:]) if clean and rest else None


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
    commanded = _NOTE_COMMAND.match(text.strip() + " ") is not None
    text = _NOTE_COMMAND.sub("", text.strip()).strip(" :,-") or text.strip()
    if commanded:  # "make a note to bring the slides" -> "Bring the slides"
        text = re.sub(r"^to\s+(?=[a-z])(?!self\b|myself\b|(?:my|the|our)\b)", "", text,
                      flags=re.IGNORECASE)
    # "make a note in voice recorder that ...": notes named without the word "notes"
    if commanded and (place := _note_place(text)) and (
            name is None or name.lower() == place[0].lower()):
        name, text = place
    text = re.sub(r"(?<!\.)\.$", "", text[:1].upper() + text[1:])  # a sentence's period
    title = re.sub(r"\s+", " ", (a.title or "").strip().strip(".\"'"))[:80] or None
    return {"text": text, "title": short_title(title, text), "kind": NOTE_ITEM,
            "owner": None, "due": None, "due_date": None, "time": None,
            "duration_minutes": None, "location": None, "list_name": name,
            "note_title": title, "source_at": source_at}


def note_word_said(ctx: Context) -> bool:
    """Does the voice note say the word "note(s)" (``_NOTE_WORD``)? Looked for wherever
    the model's transcript has words: the lines (every row of each text), the speaker
    names, and the context put before the transcript."""
    return any(_NOTE_WORD.search(x) for x in (
        ctx.context or "", *(s.speaker for s in ctx.segments),
        *(s.text for s in ctx.segments)))


def keep_asked_notes(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Only explicit commands make a note: none in a voice note without the word
    "note(s)" (``note_word_said``), and none without text."""
    asked = note_word_said(ctx)
    draft = draft.copy()
    draft.action_items = [i for i in draft.action_items
                          if i.kind != NOTE_ITEM or (asked and i.text)]
    return draft


def lone_note(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A voice note that is only one note becomes that note: its summary (written from
    all of it, so nothing the transcriber cut is lost) is the note's text, and its title
    titles an untitled note. A line for named notes ("add to my hardware notes") keeps
    its own words; several notes keep theirs."""
    draft = draft.copy()
    items, title = draft.action_items, (draft.title or "").strip()
    if len(items) != 1 or items[0].kind != NOTE_ITEM:
        return draft
    note = items[0]
    summary = " ".join((draft.summary or "").split())
    if summary and not note.list_name:
        note.text = summary
    if title and not note.note_title:
        note.note_title = title[:80]
    note.title = short_title(note.note_title, note.text)
    return draft


def undate_notes(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """A note is filed as it is: no date, time or owner."""
    draft = draft.copy()
    draft.action_items = [
        dataclasses.replace(i, due=None, due_date=None, due_time=None, time_unclear=False,
                            owner=None) if i.kind == NOTE_ITEM else i
        for i in draft.action_items]
    return draft
