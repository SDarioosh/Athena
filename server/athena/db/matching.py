"""Matching transcript lines and facts by their words (re-summaries, forgetting,
written notes, search)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from difflib import SequenceMatcher


def _line_key(text: str | None) -> str:
    """A written line's fingerprint (``written_notes.erased_lines``): the sha256 of its
    normalized words, so the same words written again are recognized."""
    return hashlib.sha256(_norm(text).encode()).hexdigest()


# A re-summary replaces a conversation's action items and facts. An old and a new item
# are the same when the text is the same, or when they come from the same transcript line
# (source_at within _SAME_LINE seconds) and read alike ("Speaker 1 is vegetarian" ->
# "Alice is vegetarian" after a rename).
_SAME_LINE = 1.0
_ALIKE = 0.6


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", (text or "").lower()).split())


# how far (seconds) from the cited line ``_redact_line`` looks for the line that says a
# forgotten fact's words
_CITE_SLACK = 30.0


def _cited_line(rows: list[sqlite3.Row], source_at: float | None,
                fact: str = "") -> sqlite3.Row | None:
    """The transcript line (of ``rows``, one conversation's segments) a fact cites.

    ``source_at`` is the conversation start plus the line's ``[N]`` in the summary prompt,
    whole seconds rounded down, so the cited line starts in ``[source_at, source_at+1)``;
    of several lines there, the one sharing most words with ``fact`` (then the first).
    Without one, the line nearest ``source_at`` within a second. When that line is
    already erased it is returned as it is (no other line is taken instead). A model
    sometimes cites the wrong line: when the line shares no word with ``fact`` (or no
    line is cited) and a line within ``_CITE_SLACK`` seconds shares at least two (all of
    a one-word fact), that line is the one."""
    words = _line_words(fact)
    line = None
    if source_at is not None:
        same = [r for r in rows if source_at <= r["started_at"] < source_at + 1]
        if same:
            line = max(same, key=lambda r: (len(words & _line_words(r["text"])),
                                            -r["started_at"]))
        else:
            near = [r for r in rows
                    if r["started_at"] < source_at + 1 and r["ended_at"] > source_at - 1]
            line = min(near, key=lambda r: abs(r["started_at"] - source_at), default=None)
    if line is not None and line["erased"]:
        return line
    if words and (line is None or not words & _line_words(line["text"])):
        near = [r for r in rows
                if source_at is None or abs(r["started_at"] - source_at) <= _CITE_SLACK]
        need = min(2, len(words))
        scored = [(len(words & _line_words(r["text"])),
                   -abs(r["started_at"] - source_at) if source_at is not None else 0, r)
                  for r in near]
        best = max((x for x in scored if x[0] >= need), default=None, key=lambda x: x[:2])
        if best is not None:
            line = best[2]
    return line


def _line_words(text: str | None) -> set[str]:
    """Content words of a line or fact, lightly stemmed ("lives" ~ "live")."""
    out = set()
    for w in _norm(text).split():
        if w in _STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w.rstrip("e") or w)
    return out


def _same(text_a: str | None, at_a: float | None, text_b: str | None,
          at_b: float | None) -> bool:
    a, b = _norm(text_a), _norm(text_b)
    if a and a == b:
        return True
    return (at_a is not None and at_b is not None and abs(at_a - at_b) <= _SAME_LINE
            and SequenceMatcher(None, a, b).ratio() >= _ALIKE)


# Words that say nothing about a topic: common English words plus the ones people use to
# point at a recording ("the one where we talked about ..."). In an OR-query they match
# nearly every conversation and bury the one that is meant.
_STOPWORDS_TEXT = """
a about after again all also am an and any are as at be because been before being but by
can could did do does doing for from had has have having he her here hers him his how i if
in into is it its just me my of on one ones or our out over she so some than that the
their them then there these they this those to too up us very was we were what when where
which while who whom why will with would you your
conversation conversations discussion discussed discussing mentioned mentioning recording
recordings said say saying talk talked talking talks thing things transcript transcripts
"""
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())
