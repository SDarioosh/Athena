"""Transcript lines, clauses and content words: what the rules read."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ...domain import CHAT, Segment

_STOP_WORDS = """a an the and or but of to in on at by for with from about as is are was
were be been being will would shall should can could may might must do does did has have had
not no now this that these those it its it's he she they we you i me my your his her their
our him them us who what when where which there here than then so very just also up out
over into after before until said says say"""
_STOP = frozenset(re.split(r"\s+", _STOP_WORDS))


def _content_words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower()):
        w = w.removesuffix("'s")
        if w in _STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w.rstrip("e") or w)
    return out


# one word of a list's or notes' name the user said: never "the", "my", "this" ...
# (``storage._LIST_PHRASE``, ``notes._NOTES_PHRASE``, the list commands)
_NAME_WORD = r"(?!(?:the|a|an|my|our|your|this|that)\b)[\w'&-]+"


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


_WHEN_WORDS = re.compile(
    r"\s*,?\s*\b(?:(?:on|by|for|this|next|at|around)\s+)*(?:"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|today|tomorrow|tonight"
    r"|\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\d{1,2}:\d{2}|noon|midday"
    r"|\d{1,2}\s+o'?\s?clock|in\s+the\s+(?:morning|afternoon|evening))\b",
    re.IGNORECASE)


def _meridiem(line: str) -> str:
    """"11 a.m." -> "11 am": the sentence splitter would end a sentence at "a." and "m."."""
    line = re.sub(r"\b([ap])\.\s?m\.(?=\s+[A-Z])", r"\1m.", line, flags=re.IGNORECASE)
    return re.sub(r"\b([ap])\.\s?m\.?", r"\1m", line, flags=re.IGNORECASE)


def _overlaps(want: set[str], text: str) -> bool:
    """``text`` says most of the content words ``want``."""
    got = _content_words(text)
    return bool(want and got and len(want & got) >= 0.6 * min(len(want), len(got)))


_CLAUSE_END = re.compile(r";\s*|,?\s+(?:and|but)\s+(?:then\s+|also\s+)?|,\s+(?:then|also)\s+",
                         re.IGNORECASE)


def clauses(lines: Sequence[Line]) -> list[tuple[str, str]]:
    """Every clause of the transcript, with its sentence."""
    out = []
    for line in lines:
        for sentence in _SENTENCE_END.split(line.text):
            sentence = sentence.strip()
            out += [(c.strip(), sentence) for c in _CLAUSE_END.split(sentence) if c.strip()]
    return out


def _said_in(text: str, spoken: list[tuple[str, str]]) -> tuple[str, str]:
    """The clause and sentence an item was written from: the clause with most of the
    item's words; empty strings when none has them."""
    want = _content_words(text)
    best, score = ("", ""), 0.0
    for clause, sentence in spoken:
        got = len(want & _content_words(clause)) / len(want) if want else 0.0
        if got > score:
            best, score = (clause, sentence), got
    return best if score >= 0.6 else ("", "")


def first_lines(lines: Sequence[Line]) -> dict[int, str]:
    """The transcript's lines by offset (seconds from the conversation start); of lines
    that start in the same second, the first."""
    out: dict[int, str] = {}
    for line in lines:
        out.setdefault(line.at, line.text)
    return out


def joined_lines(lines: Sequence[Line]) -> dict[int, str]:
    """The transcript's lines by offset, lines that start in the same second joined with
    " / " (``first_lines`` keeps the first only): what the fact review is shown as
    "said"."""
    out: dict[int, list[str]] = {}
    for line in lines:
        out.setdefault(line.at, []).append(line.text)
    return {k: " / ".join(v) for k, v in out.items()}


@dataclass(frozen=True)
class Line:
    """One transcript line as the rules read it: its whole seconds from the
    conversation start and its words."""

    at: int
    text: str


# A later row of a segment's text that itself reads like a transcript line ("[15] Me:
# text"): read as a line of its own (``transcript_lines``)
_LINE_IN_TEXT = re.compile(r"^\[(?P<at>\d+)\] [^:]*: (?P<text>.*)$")


def transcript_lines(segments: Iterable[Segment], origin: float) -> list[Line]:
    """The lines the rules read, from the segments, the way they always read the
    transcript the model is shown (``prompts.format_transcript``: "[12] Me: text", chat
    lines "[12] Dana (chat): text"):

    - a line's offset is ``max(0, int(started_at - origin))``;
    - its text is what follows the first colon of "<speaker>: <text>", which must be
      followed by a space (a speaker "Dr: Who" gives "Who: text"; a speaker "A:B" or
      "Me:" gives no line); trailing whitespace is dropped, one leading space is the
      separator's, and an empty text gives no line;
    - only the text's first row counts; a later row (a text with line breaks) counts
      only when it reads like a line itself ("[15] Me: text"), with its own offset."""
    out = []
    for s in segments:
        who = f"{s.speaker} (chat)" if s.kind == CHAT else s.speaker
        rows = f"{who}: {s.text}".splitlines()
        first = rows[0].rstrip() if rows else ""
        colon = first.find(":")
        if colon >= 0 and first[colon + 1:colon + 2] == " ":
            out.append(Line(max(0, int(s.started_at - origin)), first[colon + 2:]))
        for row in rows[1:]:
            m = _LINE_IN_TEXT.match(row.strip())
            if m:
                out.append(Line(int(m["at"]), m["text"]))
    return out
