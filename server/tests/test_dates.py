"""Due dates worked out in code from the deadline phrase and the conversation date."""

from __future__ import annotations

from datetime import date

import pytest

from athena.dates import resolve_due, resolve_time

TUE = date(2026, 9, 15)  # a Tuesday


@pytest.mark.parametrize(("phrase", "today", "want"), [
    ("Friday", TUE, "2026-09-18"),
    ("by Friday", TUE, "2026-09-18"),
    ("on Tuesday", TUE, "2026-09-15"),
    ("next Tuesday", TUE, "2026-09-22"),
    ("today", TUE, "2026-09-15"),
    ("tomorrow", TUE, "2026-09-16"),
    ("October thirty-first", date(2026, 10, 3), "2026-10-31"),
    ("by October 31st", date(2026, 10, 3), "2026-10-31"),
    ("Oct. 21", TUE, "2026-10-21"),
    ("the 8th of November", TUE, "2026-11-08"),
    ("November eighth", TUE, "2026-11-08"),
    ("January 12", date(2026, 10, 14), "2027-01-12"),  # deadlines lie ahead
    ("2026-12-01", TUE, "2026-12-01"),
    ("next week", TUE, None),
    ("soon", TUE, None),
    ("January", TUE, None),
    ("that afternoon", TUE, None),
    ("February 30", TUE, None),
    ("", TUE, None),
])
def test_resolve_due(phrase, today, want):
    got = resolve_due(phrase, today)
    assert (got.isoformat() if got else None) == want


def test_code_date_wins_over_the_model(pipeline):
    from test_insights import record, use_llm

    summary = {"title": "t", "summary": "s", "key_points": [], "people": [], "topics": [],
               "facts": [], "action_items": [
                   {"text": "Send the spreadsheet", "due": "Friday", "due_date": "2025-10-17",
                    "at_seconds": 1},  # the model counted a week too far
                   {"text": "Fix the fan", "due": "next week", "due_date": "2025-10-13",
                    "at_seconds": 1},  # vague: the model's date stays
                   {"text": "Review the pull request today", "at_seconds": 1}]}
    use_llm(pipeline, summary=summary)  # conversation on Thursday 2025-10-09, Berlin
    conv = pipeline.store.get_conversation(record(pipeline))
    assert [a["due_date"] for a in conv["action_items"]] == [
        "2025-10-10", "2025-10-13", "2025-10-09"]


@pytest.mark.parametrize(("phrase", "want"), [
    ("12 pm", ("12:00", False)),
    ("noon", ("12:00", False)),
    ("Wednesday at 12 pm", ("12:00", False)),
    ("3:30 pm", ("15:30", False)),
    ("3:30 P.M.", ("15:30", False)),
    ("6 p.m.", ("18:00", False)),
    ("3pm for an hour at Main Street", ("15:00", False)),
    ("half past two", ("14:30", False)),
    ("quarter past ten in the morning", ("10:15", False)),
    ("quarter to four", ("15:45", False)),
    ("five thirty pm", ("17:30", False)),
    ("midnight", ("00:00", False)),
    ("17:00", ("17:00", False)),
    ("07:30", ("07:30", False)),
    ("12", ("12:00", False)),             # bare hours: 12 is noon,
    ("5", ("17:00", False)),              # 1-6 the afternoon,
    ("remind me at 5 to call Mom", ("17:00", False)),
    ("by 2", ("14:00", False)),
    ("9", ("09:00", True)),               # 7-11 the morning, but maybe the evening
    ("at 10 o'clock", ("10:00", True)),
    ("9:15", ("09:15", True)),
    ("at 9 in the morning", ("09:00", False)),
    ("at 8 tonight", ("20:00", False)),
    ("7 in the evening", ("19:00", False)),
    ("11 at night", ("23:00", False)),
    ("13 pm", (None, True)),              # said, but can't be
    ("25:00", (None, True)),
    ("Wednesday", (None, False)),         # no time at all
    ("this afternoon", (None, False)),
    ("at 2 people", (None, False)),
    ("at one point", (None, False)),
    ("13 mm by 14 mm by 3 mm", (None, False)),  # measurements
    ("cut it at 5 cm", (None, False)),
    ("by 4 at the gym", ("16:00", False)),
    ("2026-10-21", (None, False)),
    ("costs 3.30", (None, False)),
    ("", (None, False)),
    (None, (None, False)),
])
def test_resolve_time(phrase, want):
    assert resolve_time(phrase) == want


# QC S4 / S10: regressions
@pytest.mark.parametrize(("phrase", "today", "want"), [
    ("February 29", date(2027, 12, 1), "2028-02-29"),  # next year is a leap year
    ("February 29", date(2026, 10, 24), None),          # neither year has one
    ("Buy two markers", TUE, None),                    # "two mar(kers)" is no date
    ("Submit one decision", TUE, None),                # "one dec(ision)"
    ("by 5 November", TUE, "2026-11-05"),
    ("the 21 Oct.", TUE, "2026-10-21"),
])
def test_resolve_due_regressions(phrase, today, want):
    got = resolve_due(phrase, today)
    assert (got.isoformat() if got else None) == want


@pytest.mark.parametrize(("phrase", "want"), [
    ("quarter to 12 pm", ("11:45", False)),  # a quarter before noon
    ("quarter to 12 am", ("23:45", False)),  # a quarter before midnight
    ("quarter to 1 am", ("00:45", False)),
    ("quarter to 1 pm", ("12:45", False)),
    ("by 5 November", (None, False)),        # a date, not 17:00
    ("by 5 Nov", (None, False)),
    ("keep error rate at 10-15%", (None, False)),  # a range, not 10:15
    ("at 10-15 people", (None, False)),
    ("at 10-15", (None, False)),              # no am/pm: a range
    ("at 5-30 pm", ("17:30", False)),         # am/pm follows: a time
    ("at 5-30pm", ("17:30", False)),
    ("at 9-45 a.m.", ("09:45", False)),
    ("at 5:30pm", ("17:30", False)),          # no space before pm
    ("at 5.30pm", ("17:30", False)),
    ("between 10-15 pm", ("22:15", False)),
    ("at five-thirty pm", ("17:30", False)),  # hyphenated words still read
    ("at 5-ish", ("17:00", False)),
])
def test_resolve_time_regressions(phrase, want):
    assert resolve_time(phrase) == want
