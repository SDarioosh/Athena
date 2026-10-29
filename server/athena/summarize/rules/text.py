"""Transcript lines, clauses and content words: what the rules read."""

from __future__ import annotations

import re

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


# "[12] Me: text": a line of format_transcript
_LINE = re.compile(r"^\[(?P<at>\d+)\] [^:]*: (?P<text>.*)$")
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


def _spoken(parts: list[str]) -> list[tuple[str, str]]:
    """Every clause of the transcript, with its sentence."""
    out = []
    for part in parts:
        for line in part.splitlines():
            m = _LINE.match(line.strip())
            if not m:
                continue
            for sentence in _SENTENCE_END.split(m["text"]):
                sentence = sentence.strip()
                out += [(c.strip(), sentence) for c in _CLAUSE_END.split(sentence)
                        if c.strip()]
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


def _said_lines(parts: list[str]) -> dict[int, str]:
    """The transcript's lines by offset, lines that start in the same second joined
    (``_lines`` keeps the first only): what the review is shown as "said"."""
    out: dict[int, list[str]] = {}
    for part in parts:
        for line in part.splitlines():
            m = _LINE.match(line.strip())
            if m:
                out.setdefault(int(m["at"]), []).append(m["text"])
    return {k: " / ".join(v) for k, v in out.items()}


def _lines(parts: list[str]) -> dict[int, str]:
    """The transcript's lines by offset (seconds from the conversation start)."""
    out: dict[int, str] = {}
    for part in parts:
        for line in part.splitlines():
            m = _LINE.match(line.strip())
            if m:
                out.setdefault(int(m["at"]), m["text"])
    return out
