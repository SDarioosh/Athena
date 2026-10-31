"""Spoken commands in a voice note: to-dos, lists and notes asked for out loud, and
which items are explicit."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

from ...dates import resolve_time
from ...domain import NOTE_ITEM, clean_list_name, is_explicit, short_title
from ..draft import Context, DraftItem, SummaryDraft
from ..schemas import ActionItem
from ..storage import _item_storage
from .notes import _NOTE_COMMAND, _NOTES_PHRASE, _note_storage, note_word_said
from .text import (
    _NAME_WORD,
    _SENTENCE_END,
    _STOP,
    _WHEN_WORDS,
    Line,
    _content_words,
    _meridiem,
    _overlaps,
    _said_in,
)

# What is said before a command and isn't part of it: fillers ("um", "okay so") and
# requests ("can you", "I want to", "let's"). Whisper writes them, and a command after
# them is still a command ("Um, make a to-do to get eggs").
_FILLER = (r"um+|uh+|uhm|erm|er|hmm+|mm+|ah|oh|okay|ok|alright|all\s+right|right|so|and"
           r"|also|then|hey|please|well|yeah|yes|now|just|actually|oh|anyway")
_LEAD = re.compile(
    rf"^(?:(?:{_FILLER})\b[\s,.!-]*"
    r"|(?:can|could|would|will)\s+you\s+(?:please\s+)?|can\s+we\s+|let's\s+|let\s+us\s+"
    r"|(?:i|we)\s+(?:want|need|have|got)\s+to\s+|i(?:'d|\s+would)\s+like\s+to\s+"
    r"|i\s+wanna\s+|go\s+ahead\s+and\s+|i'm\s+going\s+to\s+|i'll\s+)+", re.IGNORECASE)
_ADD_TO_NOTES = re.compile(r"^(?:add|put|write(?:\s+down)?)\b", re.IGNORECASE)
# "to... uh... fix the tap": an ellipsis the transcriber put into a sentence, and fillers
# inside a command, are no part of it
_ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…)\s*(?=[a-z])")
_INNER_FILLER = re.compile(r"(?<=\s)(?:um+|uh+|uhm|erm|er|hmm+)\b[\s,.]*", re.IGNORECASE)
# what may join two commands in one sentence ("make a to-do to get eggs and make a note
# that ..."): a comma or semicolon, "and", "then", "also", "plus"
_CLAUSE_JOIN = re.compile(r"\s*[,;]\s*(?:(?:and|then|also|plus)\s+)*"
                          r"|\s+(?:and|then|also|plus)\s+(?:(?:then|also)\s+)?", re.IGNORECASE)
_POINTS_BACK = re.compile(r"^(?:add|put|write)\s+(?:it|this|that)\b", re.IGNORECASE)
# thanks at the end of a command: "make a to-do to get eggs, thanks"
_THANKS = re.compile(r"[\s,]*\b(?:thanks|thank\s+you|please|cheers)\s*[.!]*$", re.IGNORECASE)

# a sentence the transcriber left open is read on into the next lines up to this length
_SENTENCE_MAX = 400


def _whole_sentences(lines: Sequence[Line]) -> list[tuple[str, int]]:
    """The transcript's sentences, each with the offset of the line it starts on. The
    transcriber breaks lines mid-sentence ("... has a bug where a" / "speaker is ..."):
    a line that doesn't end a sentence goes on into the next one."""
    out: list[tuple[str, int]] = []
    open_: tuple[str, int] | None = None  # the sentence still being said
    for line in lines:
        said = _INNER_FILLER.sub("", _ELLIPSIS.sub(" ", line.text))
        pieces = [p.strip() for p in _SENTENCE_END.split(said) if p.strip()]
        for i, piece in enumerate(pieces):
            if open_ is not None and i == 0:
                piece, start = f"{open_[0]} {piece}", open_[1]
            else:
                start = line.at
            open_ = None
            if i < len(pieces) - 1 or piece.endswith((".", "!", "?")) \
                    or len(piece) >= _SENTENCE_MAX:
                out.append((piece, start))
            else:
                open_ = (piece, start)
    if open_ is not None:
        out.append(open_)
    return out


def _starts_command(text: str) -> bool:
    """Does ``text`` start with a to-do, list or note command?"""
    t = _LEAD.sub("", text.strip())
    return bool(_TODO_COMMAND.match(t) or _list_command(t) is not None
                or _NOTE_COMMAND.match(t + " ")
                or (_ADD_TO_NOTES.match(t) and _NOTES_PHRASE.search(t)))


def _command_units(lines: Sequence[Line]) -> list[tuple[str, int]]:
    """The sentences, with one sentence that holds several commands split at each ("Make
    a to-do to get eggs and make a note that the car is broken" is a to-do and a note,
    not one to-do that says both); the lead words of each are taken off."""
    return [(c, at) for sentence, at in _whole_sentences(lines)
            for c in (_LEAD.sub("", x) for x in _split_commands(sentence)) if c]


def _split_commands(sentence: str) -> list[str]:
    """``sentence`` cut before each command that follows a comma, "and", "then" ...;
    one piece when it holds no second command."""
    out, start = [], 0
    for m in _CLAUSE_JOIN.finditer(sentence):
        head = sentence[start:m.start()].strip(" ,;")
        tail = sentence[m.end():]
        # "..., add it to the car notes" points back: one command
        if m.end() < len(sentence) and head and _starts_command(tail) \
                and not _POINTS_BACK.match(_LEAD.sub("", tail)):
            out.append(head)
            start = m.end()
    out.append(sentence[start:].strip())
    return [x for x in out if x]


def _command_sentences(lines: Sequence[Line]) -> list[tuple[str, int]]:
    """Sentences that ask for a note ("create a note ...", "note that ...", "add to my
    hardware notes ..."), with the offset of the line they start on."""
    sentences = _command_units(lines)
    out = []
    i = 0
    while i < len(sentences):
        said, at = sentences[i]
        i += 1
        if _command_only(said) and i < len(sentences):
            # "Create a note. The battery is 13 mm wide.": the note is the next sentence
            said = re.sub(r"^(add|put|write(?:\s+down)?)\s+(?:it|this|that)\b", r"\1",
                          said.rstrip(" .!?:,-"), flags=re.IGNORECASE)
            said = f"{said}: {sentences[i][0]}"
            i += 1
        if _NOTE_COMMAND.match(said + " ") or (
                _ADD_TO_NOTES.match(said) and _NOTES_PHRASE.search(said)):
            out.append((_THANKS.sub("", said), at))
    return out


def _command_only(said: str) -> bool:
    """A note command with nothing to note ("Create a note.", "Add to my hardware
    notes."): the transcriber ended the sentence before the note."""
    s = said.strip(" .!?:,-") + " "
    if _ADD_TO_NOTES.match(s) and (m := _NOTES_PHRASE.search(s)):
        rest = (s[:m.start()] + " " + s[m.end():]).strip(" ,.:-")
        return re.fullmatch(r"(?:add|put|write(?:\s+down)?)?(?:\s+(?:it|this|that))?", rest,
                            flags=re.IGNORECASE) is not None
    return _NOTE_COMMAND.match(s) is not None and not _NOTE_COMMAND.sub("", s).strip()


def _words(text: str) -> set[str]:
    """The words that say what a note is about ("13mm" = "13 mm"); not "the", "is"."""
    return set(re.findall(r"[^\W\d_]+|\d+", text.lower())) - _STOP


def _covers(item: DraftItem, want: set[str]) -> bool:
    """The item says mostly the same words."""
    got = _words(item.text)
    return bool(got) and len(want & got) >= 0.6 * min(len(want), len(got))


# a to-do command the model left in a note's text
_OTHER_COMMAND = re.compile(r"\b(?:to-?do|task|remind\s+me|reminder)\b", re.IGNORECASE)


def notes_from_commands(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Only in a voice note that says the word "note(s)" (``notes.note_word_said``):
    make sure each spoken note command gives a note: an item the model already wrote
    for it becomes the note (small models take "add to my hardware notes ..." for a
    task); a command without an item gets one, untitled (the hub titles it). A model's
    note that left out the notes the user named ("in voice recorder") goes there.
    A to-do the user asked for out loud is never taken for a note."""
    if not note_word_said(ctx):
        return draft
    draft = draft.copy()
    items = draft.action_items
    for said, at in _command_sentences(ctx.lines):
        note = DraftItem.from_row(_note_storage(ActionItem(text=said, kind=NOTE_ITEM), said,
                                                ctx.origin + at))
        want = _words(note.text)
        if not want:
            continue
        cover = next((i for i in items if i.kind == NOTE_ITEM and _covers(i, want)), None)
        if cover is not None:
            if note.list_name and not cover.list_name:
                cover.list_name, cover.text = note.list_name, note.text
            elif _OTHER_COMMAND.search(cover.text) and not _OTHER_COMMAND.search(said):
                # the model wrote the note and the to-do said with it as one note
                cover.text, cover.title, cover.note_title = note.text, note.title, None
            continue
        twin = next((i for i in items if i.kind != NOTE_ITEM and not i.due_date
                     and not i.due_time and not i.asked and _covers(i, want)), None)
        if twin is not None:
            if len(_words(twin.text)) > len(want):  # the model wrote down more of it
                text = _NOTE_COMMAND.sub("", twin.text.strip()).strip(" :,-")
                note.text = re.sub(r"(?<!\.)\.$", "", text[:1].upper() + text[1:])
                note.title = short_title(None, note.text)
            # the twin becomes the note: the note's fields, the twin's place in the list
            twin.text, twin.title, twin.kind, twin.owner = (note.text, note.title, note.kind,
                                                            note.owner)
            twin.due, twin.due_date, twin.duration_minutes = None, None, None
            twin.location, twin.list_name, twin.note_title = None, note.list_name, None
            twin.source_at = twin.source_at or ctx.origin + at
            twin.time_unclear, twin.said_time = False, None
        else:
            items.append(note)
    return draft


# A to-do the user asked for out loud ("Make a to-do to figure out how ...", "add a task:
# ...", "new to-do: ...", "remind me (on Friday) to ..."): what follows the command is the
# to-do. Not "make a to-do list" (a list header, see ``_after_list_header``).
# a day said between the command and the to-do: "make a to-do for tomorrow to ...",
# "remind me on Friday to ...", "remind me at 5 to ..."
_CMD_WHEN = (r"(?:(?:on|by|for|this|next|at|around|in\s+the)\s+)*(?:today|tonight|tomorrow"
             r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon"
             r"|evening|weekend|week|\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?|noon)"
             r"(?:\s+(?:morning|afternoon|evening|night))?"
             r"(?:\s+(?:at|around)\s+\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?)?")
_TODO_COMMAND = re.compile(
    r"^(?:(?:please|ok(?:ay)?|so|and|also|then)[,\s]+)*(?:"
    r"(?:make|create|add|set|put|write|new)\s+(?:(?:a|an|it|me)\s+)?(?:new\s+)?"
    r"(?:to-?do|to\s+do(?!\s+(?:the|my|a|an|some|it|this|that|with|about)\b)|task)"
    r"(?:\s+item)?(?!\s*lists?\b)"
    rf"(?:\s+for\s+(?P<when>{_CMD_WHEN})(?=\s*(?:,|:|-|\bto\b)))?"
    r"(?:\s*(?:to|that|for\s+me\s+to|called|named|saying|:|,|-))?"
    r"|(?:to-?do|to\s+do|task)\s*[:,-]"
    rf"|remind\s+me\s+(?:(?P<rwhen>{_CMD_WHEN})\s+)?(?:to|about))\s+(?P<what>\S.*)$",
    re.IGNORECASE)
# "add eggs to my shopping list", "put call the bank on my work to-do list", "add to the
# shopping list: oat milk", "add pick up the parcel to my to-do list"
_LIST_ITEM_FIRST = re.compile(
    r"^(?:add|put|write(?:\s+down)?)\s+(?P<what>\S.*?)\s+(?:to|on|onto|in|into)\s+"
    rf"(?:(?:my|the|our)\s+)?(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+list\b"
    r"(?P<after>.*)$", re.IGNORECASE)
_LIST_FIRST = re.compile(
    r"^(?:add|put|write(?:\s+down)?)\s+(?:(?:this|it)\s+)?(?:to|on|onto|in|into)\s+"
    rf"(?:(?:my|the|our)\s+)?(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+list\b"
    r"\s*[:,-]?\s*(?P<what>\S.*)$", re.IGNORECASE)


def _list_command(said: str) -> tuple[str, str | None, str | None] | None:
    """(what, list, when) for a spoken "add ... to my <name> list [for Friday]" (list
    None for the plain to-do list), else None. ``when``: the words after the list, if
    they name a day (the hub shows the date next to the line)."""
    s = said.strip().rstrip(".!?")
    m = _LIST_ITEM_FIRST.match(s)
    what = when = None
    if m and not re.match(r"^(?:this|it|that)$", m["what"], re.IGNORECASE):
        what, after = m["what"], m["after"].strip(" ,")
        if after and _WHEN_WORDS.sub(" ", _meridiem(after)).strip(" ,.;:-"):
            what = f"{what} {after}"  # more than a day: part of the item
        else:
            when = after or None
    elif m := _LIST_FIRST.match(s):
        what = m["what"]
    if not m or not what:
        return None
    what = re.sub(r"^(?:a|an|some)\s+", "", what, flags=re.IGNORECASE)
    return what, clean_list_name(f"{m['name']} list"), when


# "Make a to-do list (for the weekend).", "Shopping list: eggs, milk, bread.", "Make a
# shopping list.": the next short sentences (or what follows the colon) are its lines
_HEADER_VERB = re.compile(
    r"^(?:make|create|start|begin|new|write)\s+(?:(?:a|my|the|our)\s+)?(?:new\s+)?"
    rf"(?:(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+)?list\b(?P<tail>.*)$",
    re.IGNORECASE)
_HEADER_BARE = re.compile(
    rf"^(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}?)\s+list\s*:\s*(?P<tail>\S.*)$",
    re.IGNORECASE)
_HEADER_WHEN = re.compile(r"^\s*for\s+(?P<when>[^:,]+?)\s*(?=[:,]|$)", re.IGNORECASE)
_LIST_END = re.compile(r"^(?:okay|ok|alright|so|that's\s+(?:it|all)|that\s+is\s+(?:it|all)"
                       r"|thanks|thank\s+you|done|end(?:\s+of\s+(?:the\s+)?list)?)\b[\s,.!]*"
                       r"(?:that's\s+(?:it|all)[\s.!]*)?$", re.IGNORECASE)


def _list_header(said: str) -> tuple[str | None, str | None, str] | None:
    """(list, day, things said after it) when ``said`` starts a list; list None for the
    plain to-do list."""
    s = said.strip().rstrip(".!?")
    m = _HEADER_VERB.match(s) or _HEADER_BARE.match(s)
    if m is None:
        return None
    tail, when = m["tail"] or "", None
    if w := _HEADER_WHEN.match(tail):
        when, tail = w["when"], tail[w.end():]
    if m.re is _HEADER_VERB and tail.strip() and not re.match(r"^\s*[:,-]", tail):
        return None  # "make a list of what to pack" is no list header
    tail = tail.strip(" :,-")
    name = clean_list_name(f"{m['name']} list") if m["name"] else None
    return name, when, tail


def _list_things(text: str) -> list[str]:
    """"eggs, milk and bread" -> the things; "" -> none."""
    text = text.strip(" .")
    if not text:
        return []
    return [p.strip() for p in _LIST_SEP.split(text) if p.strip()]


def _add_asked(items: list[DraftItem], text: str, list_name: str | None,
               when: str | None, source_at: float, owner: str) -> None:
    """A line said for a list: a task on it, unless an item already says it (that one
    gets the list)."""
    want = _content_words(text)
    if not want:
        return
    twins = [i for i in items if i.kind != NOTE_ITEM and _overlaps(want, i.text)]
    for twin in twins:
        twin.asked = True
        if list_name and not twin.list_name:
            twin.list_name = list_name
    if not twins:
        items.append(dataclasses.replace(DraftItem.from_row(_item_storage(ActionItem(
            text=text, kind="task", owner=owner, list_name=list_name, due=when,
            title=_todo_title(text)), source_at)), asked=True))


def _todo_text(what: str, when: str | None = None) -> str:
    text = _THANKS.sub("", re.sub(r"(?<!\.)[.!?]+$", "", what.strip(" ,:-")))
    if when:
        text = f"{text} {when.strip()}"
    return text[:1].upper() + text[1:]


def _todo_title(text: str) -> str:
    """A spoken to-do's title without the day or time ("Get eggs tomorrow" -> "Get
    eggs"); the hub shows the due date next to it."""
    bare = re.sub(r"\s+", " ", _WHEN_WORDS.sub(" ", _meridiem(text))).strip(" ,.;:-")
    return short_title(None, bare if len(bare.split()) >= 2 else text)


def todos_from_commands(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """Make sure each to-do the user asked for out loud is an item. The prompts call a
    vague wish ("figure out how ...") no action item unless it has a command word, and
    small models drop it even with one (for "Make a to-do to figure out how renewing a
    fishing permit works for Bruno" they list no item, so the hub has nothing to file or
    suggest).
    A command no item already covers gets a task; the words the transcriber put on the
    next line ("... works" / "for Bruno.") are part of it. An item that covers one (the
    model reworded it) is marked as asked for (``DraftItem.asked``): it stays a to-do and is
    explicit even when its words no longer find the command's sentence. "Add ... to my
    shopping list" works the same, and gives the list to an item the model left without.
    "Remind me" with a time is a reminder."""
    draft = draft.copy()
    items, origin, owner = draft.action_items, ctx.origin, ctx.owner
    header: tuple[str | None, str | None] | None = None  # (list, day) of a list said
    for said, at in _command_units(ctx.lines):
        m = _TODO_COMMAND.match(said.strip())
        list_name = due = None
        if (got_header := _list_header(said)) is not None:
            header = got_header[:2]
            for thing in _list_things(got_header[2]):  # "Shopping list: eggs, milk"
                _add_asked(items, _todo_text(thing), header[0], header[1], origin + at, owner)
            continue
        if not m and header is not None:
            if _LIST_END.match(said) or _starts_command(said) or len(said.split()) > 8:
                header = None
            else:  # "Make a to-do list. Get eggs. Fix the car.": each one a line
                _add_asked(items, _todo_text(said), header[0], header[1], origin + at, owner)
                continue
        if m:
            what = m["what"]
            if re.match(r"remind\s+me\b.*?\babout\s", said, re.IGNORECASE):
                what = re.sub(r"^the\s+", "", what, flags=re.IGNORECASE)  # "about the dentist"
            text = _todo_text(what, m["when"] or m["rwhen"])
        elif (got := _list_command(said)) is not None:
            text, list_name, due = _todo_text(got[0]), got[1], got[2]
        else:
            continue
        want = _content_words(text)
        if not want:
            continue
        twins = [i for i in items if i.kind != NOTE_ITEM and _overlaps(want, i.text)]
        for twin in twins:
            twin.asked = True
            twin.command = (f"{text} {due or ''}".strip(),
                            bool(m and re.match(r"remind\b", said.strip(), re.IGNORECASE)))
            if list_name and not twin.list_name:
                twin.list_name = list_name
        if not twins:
            reminder = bool(m and re.match(r"remind\b", said.strip(), re.IGNORECASE)
                            and resolve_time(text)[0])
            item = ActionItem(text=text, kind="reminder" if reminder else "task",
                              owner=owner, list_name=list_name, title=_todo_title(text),
                              due=due)
            items.append(dataclasses.replace(DraftItem.from_row(_item_storage(
                item, origin + at)), asked=True))
    return draft


# the user asked for a to-do: "remind me", "todo", "to-do", "task:", "add a task", "add
# ... to my ... list", "note that". The transcriber also writes "a to-do" as "a to do" or
# "it to do" ("Make it to do to get ..."): "to do" with a space counts after make,
# create or add, or before item / list, never alone ("I need to do the dishes")
_COMMAND_WORDS = re.compile(
    r"\bremind\s+me\b|\breminder\b|\bto-?do\b|\bto\s+do\s+(?:list|item)s?\b|\btask\s*:"
    r"|\b(?:add|create|make)\s+(?:(?:a|an|it|me)\s+)?(?:new\s+)?to\s+do\b"
    r"(?!\s+(?:the|my|a|an|some|it|this|that|with|about)\b)"
    r"|\b(?:add|create|make|new)\s+(?:a\s+)?(?:new\s+)?task\b|\bnote\s+that\b"
    r"|\b(?:add|put|write)\b[^.!?]*\blist\b", re.IGNORECASE)


# A sentence that starts a to-do list ("Create a to-do list.", "Make a to do list for
# tomorrow.", "To-do list: ..."): the items said after it in the voice note are for it,
# even when the transcriber ended the sentence before them.
_LIST_HEADER = re.compile(
    r"\b(?:create|make|start|begin|write|new)\s+(?:a\s+|my\s+|the\s+)?(?:new\s+)?"
    r"(?:to-?do|to\s+do|task)s?\s+list\b|\b(?:to-?do|to\s+do|task)s?\s+list\s*:",
    re.IGNORECASE)


def _after_list_header(text: str, spoken: list[tuple[str, str]]) -> bool:
    """Was the item said in or after a sentence that starts a to-do list? An item whose
    words aren't found in the note counts too when the note has such a sentence (the
    model reworded it)."""
    header = next((i for i, (_, sentence) in enumerate(spoken)
                   if _LIST_HEADER.search(sentence)), None)
    if header is None:
        return False
    clause, sentence = _said_in(text, spoken)
    if not clause:
        return True
    return any(i >= header for i, x in enumerate(spoken) if x == (clause, sentence))


def _mark_explicit(items: list[DraftItem], spoken: list[tuple[str, str]] | None) -> None:
    """``explicit``: the user asked for the item (a date or time, a named list, a
    reminder, event or note, or in a voice note (``spoken``) a command word), so the hub
    files it; the rest are suggestions."""
    for item in items:
        said = ""
        if spoken is not None:
            said = f"{item.text}\n{_said_in(item.text, spoken)[1]}"
        item.explicit = (item.asked or is_explicit(vars(item))
                         or _COMMAND_WORDS.search(said) is not None
                         or (spoken is not None and _after_list_header(item.text, spoken)))


def mark_explicit_voice_note(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """``DraftItem.explicit`` in a voice note: command words count (``_mark_explicit``)."""
    draft = draft.copy()
    _mark_explicit(draft.action_items, ctx.spoken)
    return draft


def mark_explicit(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """``DraftItem.explicit`` in a conversation: no command words (``_mark_explicit``)."""
    draft = draft.copy()
    _mark_explicit(draft.action_items, None)
    return draft


# "eggs, milk and bread": things said as a list, each a line of its own
_LIST_SEP = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+", re.IGNORECASE)


def split_list_lines(draft: SummaryDraft, ctx: Context) -> SummaryDraft:
    """An item for a named list that says several things ("Eggs, milk and bread" for the
    shopping list) is one item per thing, so each is a line to tick. Only a comma list of
    short things ("salt and pepper" alone stays one line)."""
    out = []
    for item in draft.action_items:
        text = item.text.strip().rstrip(".")
        pieces = [p.strip() for p in _LIST_SEP.split(text) if p.strip()]
        if (item.kind != "task" or not item.list_name or "," not in text
                or len(pieces) < 2 or any(len(p.split()) > 3 for p in pieces)):
            out.append(dataclasses.replace(item))
            continue
        for piece in pieces:
            piece = piece[:1].upper() + piece[1:]
            out.append(dataclasses.replace(item, text=piece, title=short_title(None, piece)))
    return dataclasses.replace(draft.copy(), action_items=out)
