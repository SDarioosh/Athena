"""Due dates worked out in code from the deadline phrase and the conversation date."""

from __future__ import annotations

from datetime import date

import pytest

from convo_server.dates import resolve_due

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
