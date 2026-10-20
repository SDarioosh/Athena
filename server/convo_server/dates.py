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


# ---------------------------------------------------------------------- times
_HOUR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
               "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_MINUTE_WORDS = {"oh five": 5, "o five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
                 "twenty five": 25, "thirty": 30, "thirty five": 35, "forty": 40,
                 "forty five": 45, "fifty": 50, "fifty five": 55}
_HOUR_RE = r"(" + "|".join(_HOUR_WORDS) + r"|\d{1,2})"
_MIN_RE = r"(" + "|".join(sorted(_MINUTE_WORDS, key=len, reverse=True)) + r"|\d{2})"
# "at 5 people" / "at one point" are not times
_NOT_A_TIME = re.compile(r"\s*(point|of|percent|people|persons|minutes?|mins?|hours?|days?|"
                         r"weeks?|months?|years?|times|items|things|euros?|dollars?|bucks)\b")


def _hour(token: str) -> int:
    return _HOUR_WORDS.get(token) or int(token)


def _minute(token: str) -> int:
    return _MINUTE_WORDS[token] if token in _MINUTE_WORDS else int(token)


_BARE_HOURS = [  # (pattern, whole phrase must match)
    (rf"\b{_HOUR_RE}(?: {_MIN_RE})? ?o'? ?clock\b", False),
    (rf"\b{_HOUR_RE}(?: {_MIN_RE})? (?:in the |at |this )?"
     r"(?:morning|afternoon|evening|night|tonight)\b", False),
    (rf"(?:\b(?:at|by|around|until|till)|@) ?{_HOUR_RE}(?: {_MIN_RE})?\b(?![-/:.]?\d)", False),
    (rf" {_HOUR_RE}(?: {_MIN_RE})? ", True),
]


def _bare_hour(p: str) -> re.Match[str] | None:
    """An hour (and minutes) said without am/pm: "10 o'clock", "8 in the evening",
    "at 5" (not "at 5 people"), or the whole phrase "5"."""
    for pattern, whole in _BARE_HOURS:
        m = re.fullmatch(pattern, p) if whole else re.search(pattern, p)
        if m and not _NOT_A_TIME.match(p[m.end():]):
            return m
    return None


def resolve_time(phrase: str | None) -> tuple[str | None, bool]:
    """The time of day a phrase names, as ("HH:MM" 24 h, unclear).

    "12 pm"/"noon" -> 12:00, "3:30 pm" -> 15:30, "half past two" -> 14:30, "midnight" ->
    00:00, "17:00" -> 17:00. A bare hour without am/pm: 12 -> 12:00, 1-6 -> afternoon,
    7-11 -> morning but ``unclear`` (could be the evening); "in the evening", "tonight",
    "in the morning" decide. A time that can't be (e.g. "13 pm") gives (None, True).
    No time in the phrase at all gives (None, False).
    """
    if not phrase:
        return None, False
    p = " " + phrase.lower().replace("-", " ").replace("’", "'") + " "
    p = re.sub(r"\ba\.\s?m\.?", "am", p)
    p = re.sub(r"\bp\.\s?m\.?", "pm", p)
    p = re.sub(r"\s+", " ", p)
    if re.search(r"\b(noon|midday)\b", p):
        return "12:00", False
    if re.search(r"\bmidnight\b", p):
        return "00:00", False

    meridiem = r"\s*(am|pm)\b"
    hour: int | None = None
    minute = 0
    stated: str | None = None  # "am" / "pm" when said
    bare = False  # an hour said without am/pm (or a word like "evening")
    zero_padded = False
    if m := re.search(rf"\b(half|quarter) past {_HOUR_RE}\b(?:{meridiem})?", p):
        hour, minute = _hour(m.group(2)), 30 if m.group(1) == "half" else 15
        stated, bare = m.group(3), m.group(3) is None
    elif m := re.search(rf"\bquarter to {_HOUR_RE}\b(?:{meridiem})?", p):
        hour, minute = (_hour(m.group(1)) - 1) or 12, 45
        stated, bare = m.group(2), m.group(2) is None
    elif m := re.search(r"\b(\d{1,2})[:.](\d{2})\b(?:" + meridiem + ")?", p):
        if ":" not in m.group(0) and m.group(3) is None:  # "3.30" alone may be a price
            m = None
        else:
            hour, minute, stated = int(m.group(1)), int(m.group(2)), m.group(3)
            zero_padded = m.group(1).startswith("0")
            bare = stated is None and 1 <= hour <= 12 and not zero_padded
    if hour is None:
        if m := re.search(rf"\b{_HOUR_RE}(?: {_MIN_RE})?{meridiem}", p):
            hour, stated = _hour(m.group(1)), m.group(3)
            minute = _minute(m.group(2)) if m.group(2) else 0
        elif m := _bare_hour(p):
            hour = _hour(m.group(1))
            minute = _minute(m.group(2)) if m.group(2) else 0
            bare = 1 <= hour <= 12
    if hour is None:
        # "this afternoon", "later": a time was meant but none given
        return None, False
    if minute > 59 or hour > 23:
        return None, True
    unclear = False
    if stated:
        if not 1 <= hour <= 12:
            return None, True
        hour = hour % 12 + (12 if stated == "pm" else 0)
    elif bare:
        part = re.search(r"\b(morning|afternoon|evening|tonight|night)\b", p)
        if part and part.group(1) == "morning":
            hour %= 12
        elif part and (part.group(1) != "night" or hour >= 6):
            hour = hour % 12 + 12
        elif part:  # "2 at night"
            hour %= 12
        elif hour == 12:
            pass
        elif hour <= 6:
            hour += 12
        else:  # 7-11: morning, but could be the evening
            unclear = True
    return f"{hour:02d}:{minute:02d}", unclear
