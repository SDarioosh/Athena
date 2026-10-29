"""Where a voice note's commands go: a to-do, a line on a named list, a note, or a line in
named notes. The rules in code must catch each command even when the model lists no
item for it (small models often don't), and must not take one command for another.
Found by running ~100 fictional voice notes through the summarizer and the hub's filing
(bug hunt 2026-10-24)."""

from __future__ import annotations

import pytest
from conftest import fake_llm_transport

from athena.domain import Segment
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer

T0 = 1_792_800_000.0 + 8 * 3600  # Saturday 2026-10-24, 10:00 in Berlin


def items(lines: list[str], model_items: list[dict] | None = None,
          summary: str = "") -> list[dict]:
    """The stored items of a voice note; the fake model lists ``model_items`` (none by
    default: the rules alone) and writes ``summary``."""
    out = {"title": "Voice note", "summary": summary, "key_points": [], "people": [],
           "topics": [], "facts": [], "action_items": model_items or []}
    llm = LLMClient("http://llm.test/v1", "m", transport=fake_llm_transport(summary=out))
    summarizer = Summarizer(llm, "Me", timezone="Europe/Berlin", min_words=1)
    segments = [Segment(T0 + 4 * i, T0 + 4 * i + 3, said, "Me", True)
                for i, said in enumerate(lines)]
    return summarizer.summarize(segments, kind="note")["action_items"]


def one(lines: list[str], model_items: list[dict] | None = None) -> dict:
    (item,) = items(lines, model_items)
    return item


def placed(item: dict) -> tuple:
    return (item["kind"], item["text"], item["list_name"], item["explicit"])


# ------------------------------------------------------------------ notes named without "notes"
@pytest.mark.parametrize("said,name,text", [
    ("Make a note in voice recorder that case needs to be 3 mm bigger.", "voice recorder",
     "Case needs to be 3 mm bigger"),
    ("Make a note in voice recorder: case needs to be 3 mm bigger.", "voice recorder",
     "Case needs to be 3 mm bigger"),
    ("Make a note for the voice recorder that the case needs a hole for the USB port.",
     "voice recorder", "The case needs a hole for the USB port"),
    ("Make a note under car, the oil change is due at 90000 km.", "car",
     "The oil change is due at 90000 km"),
    ("Add to my car note: the left tyre is low.", "car", "The left tyre is low"),
])
def test_note_into_named_notes_without_the_word_notes(said, name, text):
    assert placed(one([said])) == ("note", text, name, True)


def test_note_split_over_lines_keeps_its_notes():
    item = one(["Make a note in voice recorder", "that the case needs to be 3 mm", "bigger."])
    assert placed(item) == ("note", "The case needs to be 3 mm bigger", "voice recorder", True)
    assert item["due_time"] is None  # "3 mm" is no time


def test_model_note_without_the_notes_gets_them():
    """The model wrote the note but left out where it goes (as Qwen3-4B does)."""
    item = one(["Make a note in voice recorder that case needs to be 3 mm bigger."],
               [{"text": "A note to record that the case needs to be 3 mm bigger.",
                 "kind": "note", "title": "Case size", "at_seconds": 0}])
    assert placed(item) == ("note", "Case needs to be 3 mm bigger", "voice recorder", True)


@pytest.mark.parametrize("said", [
    "Make a note in case I forget, the gate code is 4512.",
    "Make a note for later: the gate code is 4512.",
    "Make a note to call mom, she was sick.",
    "Make a note for Sam that the key is under the mat.",
    "Make a note in the morning that the shop opens at 9.",
    "Make a note on Friday that the shop is closed.",
    "Make a note in my notes that the gate code is 4512.",
])
def test_words_after_a_note_command_that_name_no_notes(said):
    item = one([said])
    assert (item["kind"], item["list_name"]) == ("note", None)


# ------------------------------------------------------------------ note phrasings
@pytest.mark.parametrize("said,text", [
    ("Note: the car is broken.", "The car is broken"),
    ("Note to self, the spare key is under the blue pot.",
     "The spare key is under the blue pot"),
    ("New note: the left speaker crackles at high volume.",
     "The left speaker crackles at high volume"),
    ("Add to my notes: the gate code is 4512.", "The gate code is 4512"),
    ("Um, make a note that the boiler pressure is low.", "The boiler pressure is low"),
    ("Make a note to bring the slides.", "Bring the slides"),
])
def test_note_phrasings(said, text):
    # with a to-do next to it the note keeps its own words (a lone note takes the summary)
    got = items([said, "Make a to-do to get eggs."])
    note = next(i for i in got if i["kind"] == "note")
    assert (note["text"], note["list_name"]) == (text, None)


def test_notes_is_no_command_in_a_plain_sentence():
    assert items(["Notes from the call are in the folder."]) == []


# ------------------------------------------------------------------ to-do phrasings
@pytest.mark.parametrize("said,text", [
    ("Um, make a todo to get eggs.", "Get eggs"),
    ("Hey, um, so, make a to-do to get eggs.", "Get eggs"),
    ("Alright, add a to-do to pick up the dry cleaning.", "Pick up the dry cleaning"),
    ("Can you make a to-do to call the bank?", "Call the bank"),
    ("I want to make a to-do to fix the bike brakes.", "Fix the bike brakes"),
    ("Let's make a to-do to clean out the garage.", "Clean out the garage"),
    ("New to-do: order a replacement charger.", "Order a replacement charger"),
    ("New task, back up the laptop.", "Back up the laptop"),
    ("To do: renew the car insurance.", "Renew the car insurance"),
    ("Create a to-do called replace the smoke alarm battery.",
     "Replace the smoke alarm battery"),
    ("Make a to-do to... uh... fix the leaking tap.", "Fix the leaking tap"),
    ("Make a todo to get eggs, thanks.", "Get eggs"),
    ("Can you remind me to renew my passport?", "Renew my passport"),
])
def test_todo_phrasings(said, text):
    assert placed(one([said])) == ("task", text, None, True)


@pytest.mark.parametrize("said", [
    "I need to do the dishes.",
    "I want to make it to do the garden in order.",
    "I wonder if the car needs new tyres soon.",
])
def test_no_command_no_item(said):
    assert items([said]) == []


def test_day_before_the_todo_is_its_due_date():
    item = one(["Make a to-do for tomorrow to call the vet."])
    assert (item["text"], item["title"], item["due_date"]) == (
        "Call the vet tomorrow", "Call the vet", "2026-10-25")


@pytest.mark.parametrize("said,text,kind,due,time", [
    ("Remind me at 5 to call mom.", "Call mom at 5", "reminder", "2026-10-24", "17:00"),
    ("Remind me tomorrow to take out the recycling.", "Take out the recycling tomorrow",
     "task", "2026-10-25", None),
    ("Remind me on Friday to pay the electricity bill.",
     "Pay the electricity bill on Friday", "task", "2026-10-30", None),
    ("Remind me to take the bins out at 7 pm.", "Take the bins out at 7 pm", "reminder",
     "2026-10-24", "19:00"),
    ("Remind me about the dentist on Monday.", "Dentist on Monday", "task", "2026-10-26",
     None),
])
def test_remind_me_with_the_day_first(said, text, kind, due, time):
    item = one([said])
    assert (item["text"], item["kind"], item["due_date"], item["due_time"],
            item["explicit"]) == (text, kind, due, time, True)


# ------------------------------------------------------------------ lists
@pytest.mark.parametrize("said,text,name", [
    ("Add eggs to my shopping list.", "Eggs", "shopping"),
    ("Add to my shopping list eggs.", "Eggs", "shopping"),
    ("Add to the shopping list: oat milk.", "Oat milk", "shopping"),
    ("Put batteries on the hardware store list.", "Batteries", "hardware store"),
    ("Add coffee filters to shopping list.", "Coffee filters", "shopping"),
    ("Put call the bank on my work to-do list.", "Call the bank", "work"),
    ("Um, add a hammer to my hardware list.", "Hammer", "hardware"),
    ("Add salt and pepper to my shopping list.", "Salt and pepper", "shopping"),
    # the plain to-do list is no named list: the hub's "Voice notes" list
    ("Add pick up the parcel to my to-do list.", "Pick up the parcel", None),
    ("Put call the landlord on my to do list.", "Call the landlord", None),
    ("Add to my to-do list: buy stamps.", "Buy stamps", None),
])
def test_list_commands_without_a_model_item(said, text, name):
    assert placed(one([said])) == ("task", text, name, True)


def test_model_item_without_the_list_gets_it():
    item = one(["Add oat milk to my shopping list."],
               [{"text": "Buy oat milk", "kind": "task", "at_seconds": 0}])
    assert placed(item) == ("task", "Buy oat milk", "shopping", True)


def test_day_after_the_list_is_the_due_date_not_the_text():
    item = one(["Add eggs to my shopping list for tomorrow."])
    assert (item["text"], item["list_name"], item["due_date"]) == (
        "Eggs", "shopping", "2026-10-25")
    item = one(["Add eggs to my shopping list for the cake."])
    assert (item["text"], item["due_date"]) == ("Eggs for the cake", None)


def test_several_things_for_a_list_are_one_line_each():
    got = items(["Add eggs, milk and bread to my shopping list."])
    assert [(i["text"], i["list_name"]) for i in got] == [
        ("Eggs", "shopping"), ("Milk", "shopping"), ("Bread", "shopping")]


# ------------------------------------------------------------------ several commands at once
@pytest.mark.parametrize("said,want", [
    ("Make a todo to get eggs and make a todo to get milk.",
     [("task", "Get eggs"), ("task", "Get milk")]),
    ("Make a todo to get eggs and make a note that the car is broken.",
     [("task", "Get eggs"), ("note", "The car is broken")]),
    ("Make a note that the car is broken and make a to-do to call the mechanic.",
     [("task", "Call the mechanic"), ("note", "The car is broken")]),
    ("Add eggs to my shopping list and make a todo to call the vet.",
     [("task", "Eggs"), ("task", "Call the vet")]),
])
def test_two_commands_in_one_sentence(said, want):
    assert sorted((i["kind"], i["text"]) for i in items([said])) == sorted(want)


def test_a_spoken_todo_is_never_taken_for_the_note():
    """The model listed only the to-do; the note command shares a word ("car") with it.
    Before, the to-do became the note and was lost."""
    got = items(["Make a note that the car is broken. Make a todo to fix the car."],
                [{"text": "Fix the car", "kind": "task", "at_seconds": 0}])
    assert sorted((i["kind"], i["text"]) for i in got) == [
        ("note", "The car is broken"), ("task", "Fix the car")]


def test_event_and_note_in_one_sentence():
    got = items(["Meeting with Jason on Wednesday at 12 pm, make a note to bring the slides."])
    assert sorted((i["kind"], i["text"], i["due_date"], i["due_time"]) for i in got) == [
        ("event", "Meeting with Jason", "2026-10-28", "12:00"),
        ("note", "Bring the slides", None, None)]


def test_owner_examples():
    assert placed(one(["Make a todo to get eggs."])) == ("task", "Get eggs", None, True)
    assert placed(one(["Make a todo to figure out the problem with the car."])) == (
        "task", "Figure out the problem with the car", None, True)
    note = one(["Make a note car is broken."])
    assert (note["kind"], note["list_name"]) == ("note", None)
    assert placed(one(["Make a note in voice recorder that case needs to be 3 mm bigger."])) \
        == ("note", "Case needs to be 3 mm bigger", "voice recorder", True)


# ------------------------------------------------------------------ what Qwen3-4B got wrong
# (the sandbox model: these are its outputs for the voice notes, kept as model items)
@pytest.mark.parametrize("said,model,want", [
    ("Make a to-do to get eggs tomorrow.", {"text": "Get eggs", "kind": "task"},
     ("task", "2026-10-25", None)),
    ("Make a to-do for tomorrow to call the vet.", {"text": "Call the vet", "kind": "task"},
     ("task", "2026-10-25", None)),
    ("Add a task to send the slides to Greta by Friday.",
     {"text": "Send the slides to Greta", "kind": "task"}, ("task", "2026-10-30", None)),
    ("Remind me at 5 to call mom.", {"text": "Call Mom", "kind": "task"},
     ("reminder", "2026-10-24", "17:00")),
    ("Remind me to take the bins out at 7 pm.", {"text": "Take the bins out", "kind": "task"},
     ("reminder", "2026-10-24", "19:00")),
    ("Remind me on Friday to pay the electricity bill.",
     {"text": "Pay the electricity bill", "kind": "task"}, ("task", "2026-10-30", None)),
])
def test_the_commands_day_and_time_when_the_model_drops_them(said, model, want):
    item = one([said], [{**model, "at_seconds": 0}])
    assert (item["kind"], item["due_date"], item["due_time"]) == want


def test_a_model_date_is_kept():
    item = one(["Make a to-do to get eggs tomorrow."],
               [{"text": "Get eggs", "kind": "task", "due": "Monday", "at_seconds": 0}])
    assert item["due_date"] == "2026-10-26"


def test_no_midnight_nobody_said():
    """For "remind me to call mom" the model set time 00:00: a to-do due at midnight."""
    item = one(["Remind me to call mom."],
               [{"text": "Call mom", "kind": "reminder", "time": "00:00", "at_seconds": 0}])
    assert (item["kind"], item["due_date"], item["due_time"]) == ("task", None, None)
    item = one(["Remind me at midnight to lock the shed."],
               [{"text": "Lock the shed", "kind": "reminder", "time": "midnight",
                 "at_seconds": 0}])
    assert (item["kind"], item["due_time"]) == ("reminder", "00:00")


def test_model_note_that_swallowed_the_todo():
    got = items(["Make a note that the car is broken and make a to-do to call the mechanic."],
                [{"text": "Note that the car is broken and create a to-do item to call the "
                          "mechanic.", "kind": "note", "title": "Car Broken - Call Mechanic",
                  "at_seconds": 0}])
    assert sorted((i["kind"], i["text"], i["note_title"]) for i in got) == [
        ("note", "The car is broken", None), ("task", "Call the mechanic", None)]


def test_model_task_for_the_note_said_with_a_todo():
    got = items(["Make a todo to get eggs and make a note that the car is broken."],
                [{"text": "Get eggs", "kind": "task", "at_seconds": 0},
                 {"text": "Car is broken", "kind": "task", "at_seconds": 0}])
    assert sorted((i["kind"], i["text"]) for i in got) == [
        ("note", "The car is broken"), ("task", "Get eggs")]


# ------------------------------------------------------------------ lists said as a list
@pytest.mark.parametrize("said,want", [
    ("Make a to-do list. Get eggs. Fix the car. Call the vet.",
     [("Get eggs", None, None), ("Fix the car", None, None), ("Call the vet", None, None)]),
    ("Make a to-do list for the weekend. Clean the garage. Mow the lawn. Okay, that's it.",
     [("Clean the garage", None, None), ("Mow the lawn", None, None)]),
    ("Make a to-do list for tomorrow: call the vet, get eggs.",
     [("Call the vet", None, "2026-10-25"), ("Get eggs", None, "2026-10-25")]),
    ("Shopping list: eggs, milk, bread.",
     [("Eggs", "Shopping", None), ("Milk", "Shopping", None), ("Bread", "Shopping", None)]),
    ("Make a shopping list. Eggs. Milk. Bread.",
     [("Eggs", "shopping", None), ("Milk", "shopping", None), ("Bread", "shopping", None)]),
    ("Make a list of what to pack for the trip.", []),
])
def test_list_said_line_by_line(said, want):
    got = items([said])
    assert [(i["text"], i["list_name"], i["due_date"]) for i in got] == want
    assert all(i["explicit"] and i["kind"] == "task" for i in got)


def test_list_ends_at_a_long_sentence():
    got = items(["Make a shopping list. Eggs. Milk. I really have to remember to clean the "
                 "car before the trip on the weekend."])
    assert [(i["text"], i["list_name"]) for i in got] == [("Eggs", "shopping"),
                                                          ("Milk", "shopping")]
