#!/usr/bin/env python3
"""Voice-note commands: where each of ~100 fictional voice notes' items should go.

Runs every case through the real Summarizer (the configured llama-server, or --llm) as a
voice note recorded on Saturday 2026-10-24 10:00 in Europe/Berlin, and checks the items
the hub files from: kind, list_name, a date, explicit, and a keyword in the text.

    python eval/voice_commands.py                       # CONVO_LLM_BASE_URL / CONVO_LLM_MODEL
    python eval/voice_commands.py --llm http://127.0.0.1:8080/v1 --model qwen3.5-35b
    python eval/voice_commands.py --only todo-eggs note-in-project -v

Expected placements (what the hub then does with the item):
  ("list", "Voice notes", kw)  an undated task without a list: a line on the hub's
                               "Voice notes" to-do list
  ("list", "<Name>", kw)       a task with list_name <name>: a line on that to-do list
  ("todo", None, kw)           a dated task or reminder: a to-do of its own, due then
  ("note", None, kw)           a note without list_name: an Odysseus note of its own
  ("notes", "<X> notes", kw)   a note with list_name <x>: a line in those notes
  ("cal", None, kw)            a dated event: a calendar event
  ("suggest", None, kw)        explicit false: a suggestion, not filed
An explicit item no case expects is a failure; extra suggestions are not. Each call takes
~40 s on a 4-core CPU with Qwen3-4B (the sandbox), far less on the server's GPU.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from athena.config import Settings  # noqa: E402
from athena.llm import LLMClient, Summarizer  # noqa: E402
from athena.store import Segment  # noqa: E402

T0 = 1_792_800_000.0 + 8 * 3600  # Saturday 2026-10-24 08:00 UTC = 10:00 in Berlin
V = "Voice notes"

CASES = [
    # ---------------------------------------------------------------- the owner's examples
    ("todo-eggs", ["Make a todo to get eggs."], [("list", V, "eggs")]),
    ("todo-car", ["Make a todo to figure out the problem with the car."],
     [("list", V, "car")]),
    ("note-car", ["Make a note car is broken."], [("note", None, "broken")]),
    ("note-in-project", ["Make a note in voice recorder that case needs to be 3 mm bigger."],
     [("notes", "Voice recorder notes", "3 mm")]),
    # ---------------------------------------------------------------- to-do phrasings
    ("todo-hyphen", ["Make a to-do to buy batteries."], [("list", V, "batteries")]),
    ("todo-space", ["Make a to do to buy batteries."], [("list", V, "batteries")]),
    ("todo-it-to-do", ["Make it to do to call the plumber."], [("list", V, "plumber")]),
    ("todo-no-to", ["Make a todo get the car washed."], [("list", V, "washed")]),
    ("todo-lower-nopunct", ["make a todo to get eggs"], [("list", V, "eggs")]),
    ("todo-colon", ["To-do: renew the car insurance."], [("list", V, "insurance")]),
    ("todo-colon-nohyphen", ["Todo: renew the car insurance."], [("list", V, "insurance")]),
    ("todo-add-task", ["Add a task to clean the gutters."], [("list", V, "gutters")]),
    ("todo-create-task", ["Create a task to email Priya the invoice."],
     [("list", V, "invoice")]),
    ("todo-new-todo", ["New to-do: order a replacement charger."],
     [("list", V, "charger")]),
    ("todo-new-task", ["New task, back up the laptop."], [("list", V, "laptop")]),
    ("todo-um", ["Um, make a todo to get eggs."], [("list", V, "eggs")]),
    ("todo-uh-so", ["Uh so make a to-do to sharpen the kitchen knives."],
     [("list", V, "knives")]),
    ("todo-hey", ["Hey, make a todo to water the plants."], [("list", V, "plants")]),
    ("todo-alright", ["Alright, add a to-do to pick up the dry cleaning."],
     [("list", V, "dry cleaning")]),
    ("todo-can-you", ["Can you make a to-do to call the bank?"], [("list", V, "bank")]),
    ("todo-please", ["Please add a todo to book the car service."], [("list", V, "service")]),
    ("todo-i-want", ["I want to make a to-do to fix the bike brakes."],
     [("list", V, "brakes")]),
    ("todo-lets", ["Let's make a to-do to clean out the garage."], [("list", V, "garage")]),
    ("todo-called", ["Create a to-do called replace the smoke alarm battery."],
     [("list", V, "smoke alarm")]),
    ("todo-for-me", ["Add a to-do for me to check the tyre pressure."],
     [("list", V, "tyre")]),
    ("todo-item", ["Add a to-do item to defrost the freezer."], [("list", V, "freezer")]),
    ("todo-split-lines", ["Make a to-do to figure out why the", "garage door keeps sticking."],
     [("list", V, "garage door")]),
    ("todo-two-sentences", ["Make a todo to get eggs. Make a todo to get milk."],
     [("list", V, "eggs"), ("list", V, "milk")]),
    ("todo-two-in-one", ["Make a todo to get eggs and make a todo to get milk."],
     [("list", V, "eggs"), ("list", V, "milk")]),
    ("todo-also", ["Make a to-do to get eggs. Also make a to-do to fix the fence."],
     [("list", V, "eggs"), ("list", V, "fence")]),
    ("todo-tomorrow", ["Make a to-do to get eggs tomorrow."], [("todo", None, "eggs")]),
    ("todo-for-tomorrow-front", ["Make a to-do for tomorrow to call the vet."],
     [("todo", None, "vet")]),
    ("todo-friday", ["Add a task to send the slides to Greta by Friday."],
     [("todo", None, "slides")]),
    ("todo-quantity", ["Make a to-do to buy 5 bags of mulch."], [("list", V, "mulch")]),
    ("todo-at-store", ["Make a to-do to return the drill at the hardware store."],
     [("list", V, "drill")]),
    ("todo-remind-me", ["Remind me to call mom."], [("list", V, "mom")]),
    ("remind-at-time", ["Remind me at 5 to call mom."], [("todo", None, "mom")]),
    ("remind-tomorrow-to", ["Remind me tomorrow to take out the recycling."],
     [("todo", None, "recycling")]),
    ("remind-to-at", ["Remind me to take the bins out at 7 pm."], [("todo", None, "bins")]),
    ("remind-friday", ["Remind me on Friday to pay the electricity bill."],
     [("todo", None, "electricity")]),
    ("remind-pleasecan", ["Can you remind me to renew my passport?"],
     [("list", V, "passport")]),
    ("todo-list-general", ["Add pick up the parcel to my to-do list."],
     [("list", V, "parcel")]),
    ("todo-list-put", ["Put call the landlord on my to do list."], [("list", V, "landlord")]),
    ("todo-list-colon", ["Add to my to-do list: buy stamps."], [("list", V, "stamps")]),
    ("todo-ellipsis", ["Make a to-do to... uh... fix the leaking tap."],
     [("list", V, "tap")]),
    # ---------------------------------------------------------------- named lists
    ("list-shopping", ["Add eggs to my shopping list."], [("list", "Shopping", "eggs")]),
    ("list-shopping-front", ["Add to my shopping list eggs."], [("list", "Shopping", "eggs")]),
    ("list-shopping-colon", ["Add to the shopping list: oat milk."],
     [("list", "Shopping", "oat milk")]),
    ("list-put-on", ["Put batteries on the hardware store list."],
     [("list", "Hardware store", "batteries")]),
    ("list-grocery", ["Add bread to the grocery list."], [("list", "Grocery", "bread")]),
    ("list-no-my", ["Add coffee filters to shopping list."],
     [("list", "Shopping", "coffee filters")]),
    ("list-todo-named", ["Put call the bank on my work to-do list."],
     [("list", "Work", "bank")]),
    ("list-um", ["Um, add eggs to my shopping list."], [("list", "Shopping", "eggs")]),
    ("list-two", ["Add eggs to my shopping list. Add a hammer to my hardware list."],
     [("list", "Shopping", "eggs"), ("list", "Hardware", "hammer")]),
    # ---------------------------------------------------------------- notes
    ("note-that", ["Note that the wifi password is on the router."],
     [("note", None, "router")]),
    ("note-colon", ["Note: the car is broken."], [("note", None, "broken")]),
    ("note-to-self", ["Note to self, the spare key is under the blue pot."],
     [("note", None, "blue pot")]),
    ("note-take", ["Take a note, the garage code is changed."], [("note", None, "garage")]),
    ("note-new", ["New note: the left speaker crackles at high volume."],
     [("note", None, "speaker")]),
    ("note-um", ["Um, make a note that the boiler pressure is low."],
     [("note", None, "boiler")]),
    ("note-two-sentences", ["Create a note. The battery is 13 mm wide."],
     [("note", None, "13 mm")]),
    ("note-named", ["Add to my hardware notes, the screen is 1.4 inch."],
     [("notes", "Hardware notes", "1.4")]),
    ("note-named-in", ["Make a note in my car notes that the brakes squeak."],
     [("notes", "Car notes", "brakes")]),
    ("note-named-singular", ["Add to my car note: the left tyre is low."],
     [("notes", "Car notes", "tyre")]),
    ("note-in-the", ["Make a note in the voice recorder notes that the strap broke."],
     [("notes", "Voice recorder notes", "strap")]),
    ("note-for-project", ["Make a note for the voice recorder that the case needs a hole "
                          "for the USB port."],
     [("notes", "Voice recorder notes", "usb")]),
    ("note-in-project-no-that", ["Make a note in voice recorder: case needs to be 3 mm "
                                 "bigger."],
     [("notes", "Voice recorder notes", "3 mm")]),
    ("note-under", ["Make a note under car, the oil change is due at 90000 km."],
     [("notes", "Car notes", "oil")]),
    ("note-measure-time", ["Make a note the shelf is 80 cm wide and the gap is 5 mm."],
     [("note", None, "80 cm")]),
    ("note-plain-statement", ["The wifi password is on the router."], []),
    ("note-remember", ["Remember to buy milk."], [("suggest", None, "milk")]),
    # ---------------------------------------------------------------- mixed and edge
    ("mix-todo-note", ["Make a todo to get eggs and make a note that the car is broken."],
     [("list", V, "eggs"), ("note", None, "broken")]),
    ("mix-note-todo", ["Make a note that the car is broken. Make a todo to call the "
                       "mechanic."],
     [("note", None, "broken"), ("list", V, "mechanic")]),
    ("mix-note-then-todo-same", ["Make a note that the car is broken and make a to-do to "
                                 "call the mechanic."],
     [("note", None, "broken"), ("list", V, "mechanic")]),
    ("mix-list-and-todo", ["Add eggs to my shopping list and make a todo to call the vet."],
     [("list", "Shopping", "eggs"), ("list", V, "vet")]),
    ("todo-list-header", ["Make a to-do list. Get eggs. Fix the car. Call the vet."],
     [("list", V, "eggs"), ("list", V, "car"), ("list", V, "vet")]),
    ("event", ["Meeting with the design team Tuesday 11 a.m."], [("cal", None, "design")]),
    ("event-dentist", ["Dentist appointment on Thursday at 3 pm."],
     [("cal", None, "dentist")]),
    ("todo-not-event", ["Make a to-do to cancel the dentist appointment on Thursday at 3 "
                        "pm."], [("todo", None, "cancel")]),
    ("musing", ["I wonder if the car needs new tyres soon."], []),
    ("intent", ["I should stop prioritizing side projects before the base is stable."], []),
    ("todo-trailing-thanks", ["Make a todo to get eggs, thanks."], [("list", V, "eggs")]),
    ("todo-ok-thats-it", ["Make a to-do to buy dog food. Okay, that's it."],
     [("list", V, "dog food")]),
    # ---------------------------------------------------------------- must not over-trigger
    ("fp-in-case", ["Make a note in case I forget, the gate code is 4512."],
     [("note", None, "4512")]),
    ("fp-for-later", ["Make a note for later: the gate code is 4512."], [("note", None, "4512")]),
    ("fp-note-to-call", ["Make a note to call mom, she was sick."], [("note", None, "mom")]),
    ("fp-for-person", ["Make a note for Sam that the key is under the mat."],
     [("note", None, "mat")]),
    ("fp-in-the-morning", ["Make a note in the morning that the shop opens at 9."],
     [("note", None, "shop")]),
    ("fp-on-friday", ["Make a note on Friday that the shop is closed."],
     [("note", None, "closed")]),
    ("fp-my-notes", ["Make a note in my notes that the gate code is 4512."],
     [("note", None, "4512")]),
    ("note-add-to-my-notes", ["Add to my notes: the gate code is 4512."],
     [("note", None, "4512")]),
    ("fp-dishes", ["I need to do the dishes."], []),
    ("fp-make-it-to-do", ["I want to make it to do the garden in order."], []),
    ("list-salt-pepper", ["Add salt and pepper to my shopping list."],
     [("list", "Shopping", "salt and pepper")]),
    ("fp-note-should-add", ["Make a note that I should add eggs to the recipe."],
     [("note", None, "eggs")]),
    ("lead-stack", ["Hey, um, so, make a to-do to get eggs."], [("list", V, "eggs")]),
    ("event-plus-note", ["Meeting with Jason on Wednesday at 12 pm, make a note to bring the "
                         "slides."], [("cal", None, "jason"), ("note", None, "slides")]),
    ("todo-dated-not-event", ["Make a todo to call Jason about the meeting on Wednesday at "
                              "12."], [("todo", None, "jason")]),
    ("list-header-weekend", ["Make a to-do list for the weekend. Clean the garage. Mow the "
                             "lawn."], [("list", V, "garage"), ("list", V, "lawn")]),
    ("list-header-shopping", ["Shopping list: eggs, milk, bread."],
     [("list", "Shopping", "eggs"), ("list", "Shopping", "milk"), ("list", "Shopping", "bread")]),
    ("list-three", ["Add eggs, milk and bread to my shopping list."],
     [("list", "Shopping", "eggs"), ("list", "Shopping", "milk"), ("list", "Shopping", "bread")]),
    ("remind-about", ["Remind me about the dentist on Monday."], [("todo", None, "dentist")]),
    ("multiline-note", ["Make a note in voice recorder", "that the case needs to be 3 mm",
                        "bigger."], [("notes", "Voice recorder notes", "3 mm")]),
]


def _fits(want: tuple, item: dict) -> bool:
    typ, container, kw = want
    words = " ".join(str(item.get(k) or "") for k in ("text", "title", "note_title")).lower()
    if kw.lower() not in words:
        return False
    listed = (item.get("list_name") or "").lower()
    if typ == "suggest":
        return not item["explicit"]
    if not item["explicit"]:
        return False
    if typ == "list":
        named = "" if container == V else container.lower()
        if item["kind"] != "task" or listed != named:
            return False
        return bool(named) or not item.get("due_date")  # a dated line keeps its list
    if typ == "todo":
        return (item["kind"] in ("task", "reminder") and bool(item.get("due_date"))
                and not listed)
    if typ == "note":
        return item["kind"] == "note" and not listed
    if typ == "notes":
        return item["kind"] == "note" and listed == container.lower().removesuffix(" notes")
    if typ == "cal":
        return item["kind"] == "event" and bool(item.get("due_date"))
    raise ValueError(typ)


def check(items: list[dict], expect: list[tuple]) -> bool:
    left = list(items)
    for want in expect:
        hit = next((i for i in left if _fits(want, i)), None)
        if hit is None:
            return False
        left.remove(hit)
    return not any(i["explicit"] for i in left)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--llm", help="OpenAI-compatible base URL (default CONVO_LLM_BASE_URL)")
    ap.add_argument("--model", help="model name (default CONVO_LLM_MODEL)")
    ap.add_argument("--only", nargs="*", help="case ids")
    ap.add_argument("-v", "--verbose", action="store_true", help="print passing cases too")
    args = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings(ingest_token="eval-" + "x" * 20)
    llm = LLMClient(args.llm or s.llm_base_url, args.model or s.llm_model, s.llm_api_key,
                    timeout=s.llm_timeout_seconds)
    summarizer = Summarizer(llm, "Me", timezone="Europe/Berlin")
    cases = [c for c in CASES if not args.only or c[0] in args.only]
    passed = 0
    for cid, lines, expect in cases:
        segments = [Segment(T0 + 4 * i, T0 + 4 * i + 3.5, said, "Me", True)
                    for i, said in enumerate(lines)]
        items = summarizer.summarize(segments, kind="note")["action_items"]
        ok = check(items, expect)
        passed += ok
        if not ok or args.verbose:
            print(f"{'ok  ' if ok else 'FAIL'} {cid}: {' / '.join(lines)}")
            print(f"     want {expect}")
            for i in items:
                print(f"     got  {i['kind']} explicit={i['explicit']} list={i['list_name']} "
                      f"due={i['due_date']} {i['due_time']} | {i['title']} | {i['text']}")
        sys.stdout.flush()
    print(f"{passed}/{len(cases)} voice notes routed as expected")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
