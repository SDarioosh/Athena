"""The summary while the rules work on it (``SummaryDraft``), and what they read besides
it (``Context``). Each rule is a pass ``(draft, context) -> draft`` (``passes.py``)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from ..domain import Segment
from .rules.text import Line, clauses, first_lines, joined_lines
from .schemas import KnownFacts


class FactRow(TypedDict):
    """A fact as stored (``storage.to_storage``)."""

    subject: str
    fact: str
    source_at: float | None
    kind: str


@dataclass
class DraftItem:
    """An action item on its way to storage. ``storage()`` is the stored row; the last
    three fields are the rules' own notes, never stored."""

    text: str
    title: str | None = None
    kind: str = "task"
    owner: str | None = None
    due: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    time_unclear: bool = False
    duration_minutes: int | None = None
    location: str | None = None
    list_name: str | None = None
    note_title: str | None = None
    source_at: float | None = None
    explicit: bool = False
    # the time of day as the model wrote it ("12 pm"); read once by ``resolve_due_dates``
    said_time: str | None = None
    # a spoken to-do command asked for it: it stays a to-do and is explicit
    asked: bool = False
    # (the command's words, said with "remind me"): its day and time are the command's
    # (``command_dates``)
    command: tuple[str, bool] | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DraftItem:
        """From a row of ``storage._item_storage`` / ``rules.notes._note_storage``."""
        row = dict(row)
        said_time = row.pop("time", None)
        return cls(**row, said_time=said_time)

    def storage(self) -> dict[str, Any]:
        return {"text": self.text, "title": self.title, "kind": self.kind,
                "owner": self.owner, "due": self.due, "due_date": self.due_date,
                "due_time": self.due_time, "time_unclear": self.time_unclear,
                "duration_minutes": self.duration_minutes, "location": self.location,
                "list_name": self.list_name, "note_title": self.note_title,
                "source_at": self.source_at, "explicit": self.explicit}


@dataclass
class SummaryDraft:
    """A conversation's summary between the model's answers and storage."""

    kind: str
    title: str = ""
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    action_items: list[DraftItem] = field(default_factory=list)
    facts: list[FactRow] = field(default_factory=list)

    @classmethod
    def from_storage(cls, out: dict[str, Any], kind: str) -> SummaryDraft:
        """From ``storage.to_storage``'s dict."""
        return cls(kind=kind, title=out["title"], summary=out["summary"],
                   key_points=out["key_points"], topics=out["topics"],
                   people=out["people"],
                   action_items=[DraftItem.from_row(i) for i in out["action_items"]],
                   facts=out["facts"])

    def copy(self) -> SummaryDraft:
        """A copy a pass may change without changing this draft."""
        return dataclasses.replace(
            self, key_points=list(self.key_points), topics=list(self.topics),
            people=list(self.people),
            action_items=[dataclasses.replace(i) for i in self.action_items],
            facts=[FactRow(**f) for f in self.facts])  # type: ignore[typeddict-item]

    def storage(self) -> dict[str, Any]:
        """The storage-ready dict ``Summarizer.summarize`` returns."""
        return {"title": self.title, "summary": self.summary,
                "key_points": self.key_points, "topics": self.topics,
                "people": self.people,
                "action_items": [i.storage() for i in self.action_items],
                "facts": self.facts, "kind": self.kind}


# The fact review: the facts to score (number, subject, fact, said) -> (number, score)
# pairs as the model gave them; [] when the call failed.
ReviewFacts = Callable[[list[dict[str, Any]]], list[tuple[int, int]]]


@dataclass(frozen=True)
class Context:
    """What the passes read besides the draft: the transcript and the settings."""

    segments: Sequence[Segment]
    lines: Sequence[Line]
    origin: float
    tz: ZoneInfo
    owner: str
    # the few lines put before the transcript (a meeting's topic), if any
    context: str | None = None
    known: KnownFacts | None = None
    review: ReviewFacts | None = None
    fact_min_score: int = 4
    max_facts: int = 3

    @cached_property
    def start(self) -> datetime:
        """When the conversation started, in the owner's time zone."""
        return datetime.fromtimestamp(self.origin, self.tz)

    @cached_property
    def today(self) -> date:
        return self.start.date()

    @cached_property
    def spoken(self) -> list[tuple[str, str]]:
        """Every clause of the transcript, with its sentence (``rules.text.clauses``)."""
        return clauses(self.lines)

    @cached_property
    def by_offset(self) -> dict[int, str]:
        return first_lines(self.lines)

    @cached_property
    def said_by_offset(self) -> dict[int, str]:
        return joined_lines(self.lines)


Pass = Callable[[SummaryDraft, Context], SummaryDraft]
