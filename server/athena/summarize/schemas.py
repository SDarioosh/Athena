"""The JSON schemas the summarizer sends as ``response_format`` (pydantic models)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

# "note": the user asked a voice note to save a note ("create a note ..."); voice notes only
ItemKind = Literal["task", "event", "reminder", "note"]


class ActionItem(BaseModel):
    text: str = Field(description="What needs to be done, imperative, self-contained; "
                                  "an event: its name as said, e.g. 'Meeting with Jason'")
    kind: ItemKind = Field(default="task",
                           description="event (meeting, appointment, call at a set day), "
                                       "reminder (remind me at a time), note (the user "
                                       "asked to save a note) or task")
    owner: str | None = Field(default=None, description="Who should do it, if stated")
    due: str | None = Field(default=None, description="Deadline as stated, e.g. 'Friday'")
    due_date: str | None = Field(default=None,
                                 description="The deadline as YYYY-MM-DD, if a date can be "
                                             "worked out from the conversation date")
    time: str | None = Field(default=None,
                             description="Time of day as said, e.g. '12 pm', or null")
    duration_minutes: int | None = Field(default=None,
                                         description="How long an event lasts, if said")
    location: str | None = Field(default=None, description="Where an event is, if said")
    list_name: str | None = Field(default=None,
                                  description="The user's list it goes on, if they name one "
                                              "('add milk to my shopping list' -> 'shopping')")
    title: str | None = Field(default=None,
                              description="A short imperative summary (an event: its "
                                          "name), at most 60 characters, e.g. 'Call the "
                                          "plumber about the tap'; for a note, its "
                                          "title, e.g. 'Battery dimensions'")
    at_seconds: int | None = Field(default=None,
                                   description="Offset in seconds where it was mentioned")


# Stored facts that may match some candidate facts (``Summarizer.summarize`` ``known``).
KnownFacts = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]

# What sort of fact it is; the hub labels and files facts by it (list_facts JSON ``kind``).
FACT_KINDS = ("preference", "plan", "decision", "relationship", "personal", "other")


class Fact(BaseModel):
    subject: str = Field(description="Person, project or thing the fact is about")
    fact: str = Field(description="Durable fact worth remembering months from now")
    # a plain string: a small model's odd kind must not fail the call (kept as "other")
    kind: str = Field(default="other", description="One of: " + ", ".join(FACT_KINDS))
    at_seconds: int | None = None


Kind = Literal["meeting", "call", "casual", "appointment", "other"]
DiscardReason = Literal["none", "tv_or_media", "background_noise", "gibberish",
                        "not_a_conversation"]


class Triage(BaseModel):
    """Cheap first call: is this worth summarizing, and what kind of conversation is it?"""
    discard: bool
    reason: DiscardReason
    kind: Kind


class SummaryNotes(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    people: list[str]
    topics: list[str]


class ActionItemList(BaseModel):
    action_items: list[ActionItem]


class FactList(BaseModel):
    facts: list[Fact]


class FactScore(BaseModel):
    number: int = Field(description="Number of the fact")
    score: int = Field(description="1 (leave out) to 5 (certainly worth keeping)")


class FactReview(BaseModel):
    """The second look at a conversation's facts (``Summarizer._review_facts``)."""
    scores: list[FactScore]


class FactLink(BaseModel):
    new: int = Field(description="Number of the new fact")
    relation: Literal["new", "same", "updates"]
    existing: int | None = Field(default=None, description="id of the known fact it repeats "
                                                           "or updates")


class FactLinks(BaseModel):
    links: list[FactLink]


class ConversationSummary(BaseModel):
    """Everything about one conversation (the shape before the calls were split)."""
    title: str
    summary: str
    key_points: list[str]
    action_items: list[ActionItem]
    people: list[str]
    topics: list[str]
    facts: list[Fact]


class ForgetRewrite(BaseModel):
    """A conversation's notes rewritten without facts the user erased (erase_fact)."""
    title: str
    summary: str
    key_points: list[str]
    topics: list[str]


class DailyDigest(BaseModel):
    headline: str
    summary: str
    highlights: list[str]
    open_action_items: list[str]
    people: list[str]
