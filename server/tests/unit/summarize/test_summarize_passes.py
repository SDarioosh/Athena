"""The summarizer's rules as passes: one test per pass on small inputs, the pass lists'
order, that a pass never changes the draft it is given, and that reading the lines from
segments gives exactly what reading the rendered transcript gave.

The whole-summarizer goldens are ``test_summarize_requests.py`` and
``test_summarize_rules_golden.py``.
"""

from __future__ import annotations

import copy
import random
import re
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from athena.domain import Segment
from athena.summarize import passes
from athena.summarize.draft import Context, DraftItem, FactRow, SummaryDraft
from athena.summarize.prompts import format_transcript
from athena.summarize.rules import commands, dates, errands, events, facts, intentions, notes
from athena.summarize.rules.text import (
    _CLAUSE_END,
    _SENTENCE_END,
    Line,
    clauses,
    first_lines,
    joined_lines,
    transcript_lines,
)

# Monday 2025-10-06 09:00 UTC, 11:00 in Berlin
ORIGIN = 1759741200.0


def ctx(*said: str | tuple[float, str, str], review: Any = None, known: Any = None,
        context: str | None = None, owner: str = "Me", **opts: Any) -> Context:
    """A context of the lines ``said`` (by "Me", 5 s apart, or (offset, speaker, text))."""
    segs = []
    for i, s in enumerate(said):
        at, speaker, text = s if isinstance(s, tuple) else (5 * i, "Me", s)
        segs.append(Segment(ORIGIN + at, ORIGIN + at + 3, text, speaker,
                            is_owner=speaker == owner))
    return Context(segments=segs, lines=transcript_lines(segs, ORIGIN), origin=ORIGIN,
                   tz=ZoneInfo("Europe/Berlin"), owner=owner, context=context, known=known,
                   review=review, **opts)


def draft(*items: DraftItem, facts_: list[dict[str, Any]] | None = None,
          **fields: Any) -> SummaryDraft:
    return SummaryDraft(kind=fields.pop("kind", "note"), action_items=list(items),
                        facts=[FactRow(**{"kind": "other", "source_at": None, **f})
                               for f in facts_ or []], **fields)


def texts(d: SummaryDraft) -> list[str]:
    return [i.text for i in d.action_items]


# ----------------------------------------------------------------- the pass lists
def test_note_passes_in_order() -> None:
    assert [p.__name__ for p in passes.NOTE_PASSES] == [
        "drop_passing_facts", "todos_from_commands", "resolve_due_dates", "command_dates",
        "keep_asked_notes", "notes_from_commands", "events_from_lines", "lone_note",
        "undate_notes", "drop_errands", "name_meetings", "drop_event_facts",
        "review_facts_keep_decisions", "intentions_to_facts", "split_list_lines",
        "mark_explicit_voice_note"]


def test_conversation_passes_in_order() -> None:
    assert [p.__name__ for p in passes.CONVERSATION_PASSES] == [
        "drop_passing_facts", "resolve_due_dates", "drop_errands", "name_meetings",
        "drop_event_facts", "review_facts", "mark_explicit", "drop_silent_owner"]


def rich() -> tuple[SummaryDraft, Context]:
    c = ctx("Make a to-do list.", "Get eggs.", "Make a note that the car is broken.",
            "Meeting with the design team Tuesday 11 a.m.", "Remind me at 5 to call Mom.",
            "I should stop prioritizing side projects.", "I'll grab the keys real quick.",
            review=lambda asked: [(f["number"], 3 + f["number"] % 3) for f in asked])
    d = draft(DraftItem(text="Get eggs", said_time="5 pm", source_at=ORIGIN + 5),
              DraftItem(text="Meet the design team", kind="event", due="Tuesday",
                        source_at=ORIGIN + 15),
              DraftItem(text="Call Mom", owner="Me", source_at=ORIGIN + 20),
              DraftItem(text="Stop prioritizing side projects", owner="Me",
                        source_at=ORIGIN + 25),
              DraftItem(text="Grab the keys", source_at=ORIGIN + 30),
              DraftItem(text="The car is broken", kind="note", source_at=ORIGIN + 10),
              DraftItem(text="Eggs, milk, bread", list_name="shopping"),
              facts_=[{"subject": "Me", "fact": "Me has a meeting with the design team on "
                       "Tuesday at 11 a.m.", "source_at": ORIGIN + 15},
                      {"subject": "Me", "fact": "Me was tired today."},
                      {"subject": "Bo", "fact": "Bo is the owner's brother."},
                      {"subject": "Me", "fact": "The owner decided to sell the van.",
                       "kind": "decision", "source_at": ORIGIN + 25}],
              title="Errands", summary="A to-do list and a car note.",
              people=["Bo", "Me"])
    return d, c


@pytest.mark.parametrize("step", sorted({*passes.NOTE_PASSES, *passes.CONVERSATION_PASSES},
                                        key=lambda p: p.__name__),
                         ids=lambda p: p.__name__)
def test_a_pass_never_changes_its_input(step: Any) -> None:
    d, c = rich()
    before = copy.deepcopy(d)
    step(d, c)
    assert d == before


def test_run_passes_runs_them_in_order() -> None:
    seen: list[str] = []

    def one(d: SummaryDraft, c: Context) -> SummaryDraft:
        seen.append("one")
        return d

    def two(d: SummaryDraft, c: Context) -> SummaryDraft:
        seen.append("two")
        return SummaryDraft(kind="x")

    assert passes.run_passes(draft(), ctx(), [one, two]).kind == "x"
    assert seen == ["one", "two"]


# ----------------------------------------------------------------- the draft
def test_draft_item_from_a_storage_row_and_back() -> None:
    row = {"text": "Call Ana", "title": "Call Ana", "kind": "task", "owner": "Me",
           "due": "Friday", "due_date": None, "time": "5 pm", "duration_minutes": None,
           "location": None, "list_name": None, "note_title": None, "source_at": 3.0}
    item = DraftItem.from_row(row)
    assert item.said_time == "5 pm" and row["time"] == "5 pm"
    assert list(item.storage()) == [
        "text", "title", "kind", "owner", "due", "due_date", "due_time", "time_unclear",
        "duration_minutes", "location", "list_name", "note_title", "source_at", "explicit"]


def test_draft_storage_has_the_summary_keys() -> None:
    out = draft(DraftItem(text="A"), kind="casual", title="T").storage()
    assert list(out) == ["title", "summary", "key_points", "topics", "people",
                         "action_items", "facts", "kind"]
    assert out["kind"] == "casual" and out["action_items"][0]["text"] == "A"


# ----------------------------------------------------------------- reading the lines
def test_transcript_lines_read_offsets_and_text() -> None:
    segs = [Segment(ORIGIN, ORIGIN + 1, "Hello there.  ", "Me"),
            Segment(ORIGIN + 4.9, ORIGIN + 6, "see budget.xlsx", "Dana", kind="chat"),
            Segment(ORIGIN - 2, ORIGIN, "Early.", "Me")]
    assert transcript_lines(segs, ORIGIN) == [Line(0, "Hello there."),
                                              Line(4, "see budget.xlsx"), Line(0, "Early.")]


def test_transcript_lines_keep_how_the_rendered_text_was_read() -> None:
    """Odd speakers and texts read as when the rules parsed "[n] Speaker: text"."""
    segs = [Segment(ORIGIN, 0, "a\nb", "Me"),                    # first row only
            Segment(ORIGIN + 1, 0, "x\n[9] Bo: later", "Me"),     # a row that looks like a line
            Segment(ORIGIN + 2, 0, "text", "Dr: Who"),           # the first ": " splits
            Segment(ORIGIN + 3, 0, "text", "A:B"),               # no ": " after the colon
            Segment(ORIGIN + 4, 0, "   ", "Me"),                 # no text
            Segment(ORIGIN + 5, 0, "  lead", "Me")]              # the separator takes one space
    assert transcript_lines(segs, ORIGIN) == [
        Line(0, "a"), Line(1, "x"), Line(9, "later"), Line(2, "Who: text"), Line(5, "  lead")]


def test_first_and_joined_lines() -> None:
    lines = [Line(0, "a"), Line(0, "b"), Line(3, "c")]
    assert first_lines(lines) == {0: "a", 3: "c"}
    assert joined_lines(lines) == {0: "a / b", 3: "c"}


def test_clauses() -> None:
    assert clauses([Line(0, "Get eggs and call Bo. Then rest; done")]) == [
        ("Get eggs", "Get eggs and call Bo."), ("call Bo.", "Get eggs and call Bo."),
        ("Then rest", "Then rest; done"), ("done", "Then rest; done")]


# What the rules did before they read segments: they parsed the rendered transcript
# (``format_transcript`` per part, the context before each part). Kept here as the oracle.
_OLD_LINE = re.compile(r"^\[(?P<at>\d+)\] [^:]*: (?P<text>.*)$")


def _old_rows(parts: list[str]) -> list[tuple[int, str]]:
    return [(int(m["at"]), m["text"]) for part in parts for line in part.splitlines()
            if (m := _OLD_LINE.match(line.strip()))]


def _old_lines(parts: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for at, text in _old_rows(parts):
        out.setdefault(at, text)
    return out


def _old_said_lines(parts: list[str]) -> dict[int, str]:
    out: dict[int, list[str]] = {}
    for at, text in _old_rows(parts):
        out.setdefault(at, []).append(text)
    return {k: " / ".join(v) for k, v in out.items()}


def _old_spoken(parts: list[str]) -> list[tuple[str, str]]:
    out = []
    for _, text in _old_rows(parts):
        for sentence in _SENTENCE_END.split(text):
            sentence = sentence.strip()
            out += [(c.strip(), sentence) for c in _CLAUSE_END.split(sentence) if c.strip()]
    return out


_PIECES = ["Make a to-do to get eggs", "note", "Notes", ":", ": ", " ", "\n", "\r\n", "\r",
           "\x0b", " ", "[3] Bo: hi", "[12] ", ".", "...", "a.m.", "Remind me at 5",
           "\t", "and", ", then", "Meeting with Jo Tuesday 11", "?", "Dr: ", "", "é"]


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 6)))


@pytest.mark.parametrize("seed", range(200))
def test_reading_segments_equals_reading_the_rendered_transcript(seed: int) -> None:
    rng = random.Random(seed)
    segs = [Segment(ORIGIN + rng.choice([-3, 0, 0.4, 1, 1.9, 7, 7.5, 60, 3600]),
                    ORIGIN + 9000, _random_text(rng),
                    rng.choice(["Me", "Bo", "", "Dr: Who", "A:B", "Me:", "Notes", "x\ny",
                                _random_text(rng)]),
                    kind=rng.choice(["speech", "chat"]))
            for _ in range(rng.randint(1, 12))]
    context = rng.choice([None, "This is an online meeting.\nParticipants: Bo, Me."])
    size = rng.randint(1, len(segs))
    parts = [format_transcript(segs[i:i + size], ORIGIN) for i in range(0, len(segs), size)]
    if context:
        parts = [f"{context}\n{p}" for p in parts]

    lines = transcript_lines(segs, ORIGIN)
    assert [(ln.at, ln.text) for ln in lines] == _old_rows(parts)
    assert first_lines(lines) == _old_lines(parts)
    assert joined_lines(lines) == _old_said_lines(parts)
    assert clauses(lines) == _old_spoken(parts)
    c = Context(segments=segs, lines=lines, origin=ORIGIN, tz=ZoneInfo("UTC"), owner="Me",
                context=context)
    assert notes.note_word_said(c) == (notes._NOTE_WORD.search("\n".join(parts)) is not None)


def test_note_word_counts_in_a_speaker_name() -> None:
    """Kept from when the rules searched the rendered transcript: a speaker named "Notes"
    makes every voice note say "notes"."""
    assert notes.note_word_said(ctx((0, "Notes", "Buy eggs.")))
    assert not notes.note_word_said(ctx((0, "Me", "Buy eggs.")))


# ----------------------------------------------------------------- one test per pass
def test_drop_passing_facts() -> None:
    d = draft(facts_=[{"subject": "Me", "fact": "Me was tired today."},
                      {"subject": "Ana", "fact": "Ana might move to Porto."},
                      {"subject": "Ana", "fact": "Ana is curious to see how the demo looks."},
                      {"subject": "Me", "fact": "Me wants Bo to send the report."},
                      {"subject": "Ana", "fact": "Ana likes jazz."}], people=["Bo"])
    assert [f["fact"] for f in facts.drop_passing_facts(d, ctx()).facts] == ["Ana likes jazz."]


def test_todos_from_commands() -> None:
    c = ctx("Make a to-do to get eggs tomorrow.", "Add oat milk to my shopping list.",
            "Make a to-do list.", "Fix the car.")
    d = commands.todos_from_commands(draft(DraftItem(text="Get eggs")), c)
    assert [(i.text, i.list_name, i.asked, i.command) for i in d.action_items] == [
        ("Get eggs", None, True, ("Get eggs tomorrow", False)),
        ("Oat milk", "shopping", True, None),
        ("Fix the car", None, True, None)]


def test_resolve_due_dates() -> None:
    d = dates.resolve_due_dates(draft(DraftItem(text="Call Bo", due="Friday", said_time="9"),
                                      DraftItem(text="Call Al at 8 am"),
                                      DraftItem(text="Call Jo", said_time="soonish")), ctx())
    assert [(i.due_date, i.due_time, i.time_unclear, i.said_time) for i in d.action_items] == [
        ("2025-10-10", "09:00", True, None),  # "9": morning or evening?
        ("2025-10-07", "08:00", False, None),  # 8 am had passed at 11:00: tomorrow
        (None, None, True, None)]


def test_command_dates() -> None:
    d = dates.command_dates(draft(
        DraftItem(text="Call Mom", command=("Call Mom at 5", True)),
        DraftItem(text="Call the bank", kind="reminder", due_time="00:00",
                  due_date="2025-10-07", command=("Call the bank", True)),
        DraftItem(text="Get eggs", due_date="2025-10-09", command=("Get eggs tomorrow", False)),
        DraftItem(text="A note", kind="note", command=("A note tomorrow", False))), ctx())
    assert [(i.kind, i.due_date, i.due_time, i.command) for i in d.action_items] == [
        ("reminder", "2025-10-06", "17:00", None),
        ("task", None, None, None),
        ("task", "2025-10-09", None, None),
        ("note", None, None, None)]


def test_keep_asked_notes() -> None:
    d = draft(DraftItem(text="The code is 4512", kind="note"),
              DraftItem(text="", kind="note"), DraftItem(text="Get eggs"))
    assert texts(notes.keep_asked_notes(d, ctx("The code is 4512."))) == ["Get eggs"]
    assert texts(notes.keep_asked_notes(d, ctx("Note: the code is 4512."))) == [
        "The code is 4512", "Get eggs"]


def test_notes_from_commands() -> None:
    c = ctx("Make a note in voice recorder that the case needs a new hinge.",
            "Create a note.", "The battery is 13 mm wide.")
    d = draft(DraftItem(text="Case needs a new hinge", owner="Me", said_time="x"))
    out = commands.notes_from_commands(d, c)
    assert [(i.kind, i.text, i.list_name, i.owner, i.said_time) for i in out.action_items] == [
        ("note", "The case needs a new hinge", "voice recorder", None, None),
        ("note", "The battery is 13 mm wide", None, None, None)]
    assert commands.notes_from_commands(d, ctx("The case needs a hinge.")) is d


def test_events_from_lines() -> None:
    c = ctx("Meeting with the Meshtastic team Tuesday 11 a.m.",
            "Dentist appointment Friday at 3 pm.", "I had a call with Bo on Monday at 9.")
    d = events.events_from_lines(draft(DraftItem(text="Dentist appointment")), c)
    assert [(i.kind, i.text, i.due_date, i.due_time, i.owner) for i in d.action_items] == [
        ("event", "Dentist appointment", "2025-10-10", "15:00", None),
        ("event", "Meeting with the Meshtastic team", "2025-10-07", "11:00", "Me")]


def test_lone_note() -> None:
    d = draft(DraftItem(text="Low pressure", kind="note"), title="Boiler",
              summary="The boiler  pressure is low.")
    (note,) = notes.lone_note(d, ctx()).action_items
    assert (note.text, note.note_title, note.title) == (
        "The boiler pressure is low.", "Boiler", "Boiler")
    two = draft(DraftItem(text="A", kind="note"), DraftItem(text="B", kind="note"),
                summary="S")
    assert texts(notes.lone_note(two, ctx())) == ["A", "B"]


def test_undate_notes() -> None:
    d = notes.undate_notes(draft(
        DraftItem(text="N", kind="note", due="Friday", due_date="2025-10-10",
                  due_time="09:00", time_unclear=True, owner="Me"),
        DraftItem(text="T", due_date="2025-10-10", owner="Me")), ctx())
    assert [(i.due, i.due_date, i.due_time, i.time_unclear, i.owner)
            for i in d.action_items] == [(None, None, None, False, None),
                                         (None, "2025-10-10", None, False, "Me")]


def test_drop_errands() -> None:
    c = ctx("I'll grab the keys real quick.", "I'll move my truck right now.",
            "Let me check with my wife and get back to you.")
    d = draft(DraftItem(text="Grab the keys"),
              DraftItem(text="Walk truck to the street", source_at=ORIGIN + 5),
              DraftItem(text="Check with wife"),
              DraftItem(text="Grab the keys", list_name="errands"))
    assert texts(errands.drop_errands(d, c)) == ["Check with wife", "Grab the keys"]


def test_name_meetings() -> None:
    c = ctx("Meeting with the design team on Thursday at 2 pm.")
    d = events.name_meetings(draft(
        DraftItem(text="Meet the design team", kind="event", title="Meet the design team"),
        DraftItem(text="Meet Ana", kind="event")), c)
    assert [(i.text, i.title) for i in d.action_items] == [
        ("Meeting with the design team", "Meeting with the design team"),
        ("Meet Ana", None)]


def test_drop_event_facts() -> None:
    d = draft(DraftItem(text="Meeting with the design team", kind="event",
                        due_date="2025-10-09"),
              facts_=[{"subject": "Me", "fact": "Me meets the design team on Thursday."},
                      {"subject": "Me", "fact": "Me's design team is in Leipzig."}])
    assert [f["fact"] for f in events.drop_event_facts(d, ctx()).facts] == [
        "Me's design team is in Leipzig."]


def test_review_facts() -> None:
    shown: list[Any] = []

    def review(asked: list[dict[str, Any]]) -> list[tuple[int, int]]:
        shown.extend(asked)
        return [(0, 9), (1, 2), (2, 4), (3, 5), (4, 5), (99, 5)]

    c = ctx((0, "Me", "Ana likes jazz."), (0, "Bo", "Yes."), review=review, max_facts=2,
            known=lambda fs: [{"subject": "Tom", "fact": "Tom lives in Austria."}])
    d = draft(facts_=[{"subject": "Ana", "fact": "Ana likes jazz.", "source_at": ORIGIN},
                      {"subject": "Bo", "fact": "Bo is tall."},
                      {"subject": "Tom", "fact": "Tom lives in Graz, Austria."},
                      {"subject": "Eva", "fact": "Eva plays chess."},
                      {"subject": "Me", "fact": "The owner decided to sell the van.",
                       "kind": "decision"}])
    out = facts.review_facts(d, c)
    assert shown[0] == {"number": 0, "subject": "Ana", "fact": "Ana likes jazz.",
                        "said": "Ana likes jazz. / Yes."}
    # 0 (5) and 3 (5) are the best two; 2 repeats a known fact, never capped; 1 scored 2
    assert [f["subject"] for f in out.facts] == ["Ana", "Tom", "Eva"]
    kept = facts.review_facts_keep_decisions(d, c)
    assert [f["subject"] for f in kept.facts] == ["Ana", "Tom", "Eva", "Me"]


def test_review_facts_without_a_review_keeps_the_best_by_order() -> None:
    d = draft(facts_=[{"subject": "X", "fact": f"X likes {n}."} for n in "abcd"])
    out = facts.review_facts(d, ctx(review=lambda asked: []))
    assert [f["fact"] for f in out.facts] == ["X likes a.", "X likes b.", "X likes c."]


def test_intentions_to_facts() -> None:
    c = ctx("I should stop prioritizing side projects.", "I should email Bob.",
            "Figure out how it could use less battery.")
    d = intentions.intentions_to_facts(draft(
        DraftItem(text="Stop prioritizing side projects", owner="Me", source_at=ORIGIN),
        DraftItem(text="Email Bob"),
        DraftItem(text="Figure out how it could use less battery", asked=True)), c)
    assert texts(d) == ["Email Bob", "Figure out how it could use less battery"]
    assert d.facts == [{"subject": "Me", "fact": "The owner decided to stop prioritizing "
                        "side projects.", "kind": "decision", "source_at": ORIGIN}]


def test_split_list_lines() -> None:
    d = commands.split_list_lines(draft(
        DraftItem(text="Eggs, milk and bread.", list_name="shopping", asked=True),
        DraftItem(text="Salt and pepper", list_name="shopping"),
        DraftItem(text="Eggs, milk", kind="event", list_name="shopping")), ctx())
    assert [(i.text, i.title, i.asked) for i in d.action_items] == [
        ("Eggs", "Eggs", True), ("Milk", "Milk", True), ("Bread", "Bread", True),
        ("Salt and pepper", None, False), ("Eggs, milk", None, False)]


def test_mark_explicit() -> None:
    c = ctx("Make a to-do list.", "Fix the car.", "Maybe paint the shed.")
    items = (DraftItem(text="Fix the car"), DraftItem(text="Paint the shed"),
             DraftItem(text="Get stamps", asked=True), DraftItem(text="Call Bo", kind="event"))
    voice = commands.mark_explicit_voice_note(draft(*items), c)
    conv = commands.mark_explicit(draft(*items), c)
    assert [i.explicit for i in voice.action_items] == [True, True, True, True]
    assert [i.explicit for i in conv.action_items] == [False, False, True, True]


def test_drop_silent_owner() -> None:
    d = draft(people=["Ana", "me", "Me"])
    assert passes.drop_silent_owner(d, ctx((0, "Ana", "Hi."))).people == ["Ana"]
    assert passes.drop_silent_owner(d, ctx("Hi.")) is d
