"""A to-do the user asks for out loud in a voice note ("Make a to-do to figure out how
renewing a fishing permit works for Bruno") is an item even when the model lists none: the
prompts call "figure out how ..." a vague wish, and small models drop it despite the
command word, so the hub had nothing to file or suggest."""

from __future__ import annotations

from conftest import fake_llm_transport

from athena.llm import LLMClient, Summarizer
from athena.store import Segment

T0 = 1_760_000_000.0


def items(lines: list[str], model_items: list[dict]) -> list[dict]:
    summary = {"title": "Fishing permit", "summary": " ".join(lines), "key_points": [],
               "people": [], "topics": [], "facts": [], "action_items": model_items}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=summary))
    summarizer = Summarizer(llm, "Me", timezone="Europe/Berlin", min_words=1)
    segments = [Segment(T0 + 5 * i, T0 + 5 * i + 4, said, "Me", True)
                for i, said in enumerate(lines)]
    return summarizer.summarize(segments, kind="note")["action_items"]


def test_missed_todo_command_becomes_an_explicit_task():
    (item,) = items(["Make a to-do to figure out how renewing a fishing permit works",
                     "for Bruno."], [])
    assert (item["kind"], item["text"]) == (
        "task", "Figure out how renewing a fishing permit works for Bruno")
    assert item["explicit"] is True and item["owner"] == "Me"
    assert item["source_at"] == T0 and item["due_date"] is None


def test_transcriber_spelling_and_remind_me():
    (a,) = items(["Make it to do to call the plumber about the tap."], [])
    assert a["text"] == "Call the plumber about the tap" and a["explicit"] is True
    (b,) = items(["Remind me to renew the passport on Friday."], [])
    assert b["text"] == "Renew the passport on Friday" and b["due_date"] is not None


def test_model_item_is_not_doubled():
    (item,) = items(["Make a to-do to figure out how renewing a fishing permit works",
                     "for Bruno."],
                    [{"text": "Figure out the fishing permit renewal for Bruno",
                      "kind": "task", "at_seconds": 0}])
    assert item["text"] == "Figure out the fishing permit renewal for Bruno"
    assert item["explicit"] is True


def test_no_command_no_item():
    assert items(["I wonder how renewing a fishing permit works."], []) == []
    assert items(["Make a to-do list."], []) == []


def test_reworded_vague_item_stays_a_todo():
    (item,) = items(["Make a to-do to figure out how renewing a fishing permit works",
                     "for Bruno."],
                    [{"text": "Find out how Bruno can renew the fishing permit",
                      "kind": "task", "at_seconds": 0}])
    assert item["kind"] == "task" and item["explicit"] is True
