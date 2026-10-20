"""Turn a deadline as it was said ("by Friday", "October thirty-first") into a date.

Small local models are unreliable at date arithmetic, so action-item due dates are
worked out here from the phrase and the conversation date. Only phrases that name a day
unambiguously are resolved; anything vague ("next week", "soon") gives None and the
model's own ``due_date`` is kept.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_ONES = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
         "seventh": 7, "eighth": 8, "ninth": 9, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
          "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
          "nineteenth": 19, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19, "twentieth": 20, "thirtieth": 30}
_TENS = {"twenty": 20, "thirty": 30}


def _day_number(text: str) -> int | None:
    """'21', '21st', 'twenty-first', 'twenty first', 'eighth' -> day of month."""
    text = text.strip().lower().replace("-", " ")
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", text)
    if m:
        return int(m.group(1))
    words = text.split()
    if len(words) == 1:
        return _ONES.get(words[0]) or _TEENS.get(words[0]) or _TENS.get(words[0])
    if len(words) == 2 and words[0] in _TENS and words[1] in _ONES:
        return _TENS[words[0]] + _ONES[words[1]]
    return None


_DAY = (r"(\d{1,2}(?:st|nd|rd|th)?|(?:twenty|thirty)[- ](?:first|second|third|fourth|"
        r"fifth|sixth|seventh|eighth|ninth|one|two|three|four|five|six|seven|eight|nine)|"
        + "|".join(sorted([*_ONES, *_TEENS, *_TENS], key=len, reverse=True)) + r")")
_MONTH = r"(" + "|".join(MONTHS + [m[:3] for m in MONTHS]) + r")\.?"


def _month(name: str) -> int:
    name = name.lower().rstrip(".")
    return next(i for i, m in enumerate(MONTHS, 1) if m.startswith(name[:3]))


def _future(year: int, month: int, day: int, today: date) -> date | None:
    """That day in ``year``, or next year if it's already well past (deadlines are ahead)."""
    for y in (year, year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d >= today - timedelta(days=7):
            return d
    return None


def resolve_due(phrase: str | None, today: date) -> date | None:
    """The date a deadline phrase names, counted from ``today`` (the conversation date)."""
    if not phrase:
        return None
    p = " " + re.sub(r"[,]", " ", phrase.lower()) + " "
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", p):
        return today
    if re.search(r"\btomorrow\b", p):
        return today + timedelta(days=1)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", p)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # "October 31st", "Oct thirty-first", "31 October", "the 8th of November"
    m = (re.search(rf"\b{_MONTH}\s+(?:the\s+)?{_DAY}\b", p)
         or re.search(rf"\b(?:the\s+)?{_DAY}\s+(?:of\s+)?{_MONTH}", p))
    if m:
        a, b = m.group(1), m.group(2)
        month, day = (a, b) if not a[0].isdigit() and a.rstrip(".")[:3] in [
            x[:3] for x in MONTHS] else (b, a)
        n = _day_number(day)
        return _future(today.year, _month(month), n, today) if n else None
    # "Friday", "by Friday", "on Monday", "next Tuesday" (not "next week")
    m = re.search(r"\b(next\s+|this\s+)?(" + "|".join(WEEKDAYS) + r")\b", p)
    if m:
        ahead = (WEEKDAYS.index(m.group(2)) - today.weekday()) % 7
        if m.group(1) and m.group(1).strip() == "next" and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    return None
