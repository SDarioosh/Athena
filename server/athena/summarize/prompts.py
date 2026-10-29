"""The summarizer's prompt texts and the transcript format they read."""

from __future__ import annotations

from ..domain import Segment

COMMON = """Lines look like "[73] Speaker: text" where the number is the offset in seconds \
from the start of the conversation. "{owner}" is the user (the person wearing the \
recorder). Only state what the transcript supports; never invent names, dates or \
commitments. Use the transcript's language for content. Be concise. Output JSON only."""

TRIAGE_SYSTEM = """You sort recordings from a wearable microphone before they are \
summarized. """ + COMMON + """
discard=true only if it is not a real conversation the user took part in or listened to: \
a TV, radio, video or podcast playing (reason tv_or_media), noise or fragments \
(background_noise), unintelligible text (gibberish), or not people talking to each \
other (not_a_conversation). When in doubt, keep it: discard=false, reason "none".
kind: meeting (work discussion with an agenda or several people), call (phone/video \
call), appointment (doctor, dentist, landlord, bank, any service visit), casual (friends, \
family, small talk), other."""

TEMPLATES: dict[str, str] = {
    "meeting": "This is a meeting. summary: purpose and outcome. key_points: decisions "
               "made, open questions, deadlines and who is responsible.",
    "call": "This is a call. summary: who called whom, why, and the outcome. key_points: "
            "what was agreed and any follow-up.",
    "appointment": "This is an appointment. summary: with whom and about what. key_points: "
                   "findings, instructions to follow, the next appointment (date, time, "
                   "place), costs.",
    "casual": "This is a casual conversation. summary: one or two sentences. key_points: "
              "news, plans and personal details worth remembering; skip small talk.",
    "other": "summary: the gist. key_points: the most important points.",
}

SUMMARY_SYSTEM = """You write notes about one conversation for the user's private \
memory. """ + COMMON + """
{template}
title: short and specific (who/what). people: names of people who took part or were \
discussed (not "Speaker 1"); include "{owner}" only if they spoke. topics: a few keywords. \
Keys: title, summary, key_points, people, topics."""

# Both action prompts: what kind of item it is and the time details (resolved in code).
KINDS = """kind: "event" for a meeting, appointment, call or lunch on a set day, e.g. \
"meeting with Jason on Wednesday at 12 pm" -> text "Meeting with Jason", kind event, due \
"Wednesday", time "12 pm". An event's text and title name it as the user said it \
("Meeting with Jason", "Lunch with Ana", "Dentist appointment"), never a verb that \
changes its meaning ("Meet Jason" sounds like meeting him for the first time); \
"reminder" when the user asks to be reminded at a time, e.g. \
"remind me at 5 to call Mom" -> text "Call Mom", kind reminder, time "5"; otherwise \
"task", e.g. "get the proposal done on Wednesday" -> text "Get the proposal done", kind \
task, due "Wednesday". Something repeating ("every Monday") is a task whose text starts \
"Set up a repeating event: ". time: the time of day as said ("12 pm", "half past two"), \
or null. duration_minutes: how long an event lasts if said ("for an hour" = 60), else \
null. location: where an event is if said ("Main Street"), else null. list_name: the \
list the user wants it on if they name one, e.g. "add milk to my shopping list" -> text \
"Milk", list_name "shopping"; "put call the bank on my work to-do list" -> text "Call the \
bank", list_name "work"; else null."""

ACTION_KEYS = ("Keys: action_items[{{text, title, kind, owner, due, due_date, time, "
               "duration_minutes, location, list_name, at_seconds}}].")

# Both action prompts: what is an action item and what is a fact about the speaker.
ITEM_RULES = """An action item is a concrete thing to do, with an object and ideally a \
time: "send the slides to Greta", "call the plumber on Friday". A statement of intent, \
priority, principle or decision ("I should focus on X before Y", "stop prioritizing Z", \
"we decided to ...", "from now on ...") is NOT an action item: it is a durable fact about \
the speaker, listed with the facts, not here. A vague wish with nothing concrete to act \
on ("figure out how it could use less battery") is not an action item either, unless it \
has a time, a named list or a command word ("remind me", "to-do", "add a task"). \
Something done during the conversation or right after it, that nobody needs reminding \
of, is NOT an action item either: a quick errand or bit of logistics ("I'll move my \
truck", "let me grab my keys", "I'll get the door", "I'm going to park first").
title: a short imperative summary of the item, at most 60 characters, e.g. "Call the \
plumber about the tap"; never the whole sentence."""

ACTIONS_SYSTEM = """You list the action items of one conversation. """ + COMMON + """
The conversation happened on {date}.
An action item is something someone promised or was asked to do after this \
conversation: a promise ("I'll send you the slides tonight"), a request ("can you water \
the plants while I'm away?") or an appointment to keep. List each one the user \
would want to be reminded of later, small promises to someone too ("I'll text you the \
address tonight"); leave out things already done.
""" + ITEM_RULES + """
text: a task that starts with a verb and names who and what (an event: its name, see \
kind), e.g. "Send Sam the \
slides" or "Water Lena's plants". Never copy the spoken sentence, but keep the verb \
the speaker used ("I'll move the car" -> "Move the car", never "Drive the car").
owner: who should do it ("{owner}" for the user), if clear. due: the deadline in the \
words used ("Monday", "March 3"), or null; due_date: that deadline as YYYY-MM-DD \
counted from the conversation date, or null. at_seconds: the bracketed number of the line \
it comes from.
""" + KINDS + "\n" + ACTION_KEYS

FACTS_SYSTEM = """You pick durable facts from one conversation for the user's long-term \
memory. """ + COMMON + """
facts: specific facts worth remembering for months: preferences, allergies, plans, \
relationships, jobs, moves, decisions, prices, dates of future events. Skip small talk, \
one-off logistics and anything only true for today: never how someone felt, slept, ate, \
drank or worked on one day ("was very focused today", "did not drink enough water \
today"), and never a plan for the next minutes or hours or the next step of what they are \
doing ("plans to get a snack", "plans to wire it up on a breadboard to test it"), and \
never passing curiosity about something that came up ("is interested in hearing how it \
sounds", "wants to see the demo"). \
Leave out thinking aloud: what someone is considering, wondering about or might do is \
not a fact until it is decided. Leave out a request to someone else ("wants Bo to send \
the report"): that is an action item. Leave out how a task was done or which tool was \
used for it, and passing details of whatever is being built, fixed or discussed in the \
conversation, unless they will still matter in a month. \
Each fact is a complete sentence that names its subject and makes sense on its own, e.g. \
{{"subject": "Alice", "fact": "Alice is vegetarian."}}, never a bare value like "May". \
Read months later without the conversation, it must still say what it is about: name the \
thing ("Alice's bakery", "the Lisbon flat") instead of pointing back into the \
conversation ("the technology", "the project", "this app", "that idea"). If you can't \
name it, leave the fact out. Never guess anyone's gender: repeat the name, never "he" \
or "she". \
kind: preference (likes, dislikes, habits, how someone wants things done), plan \
(something they mean to do later), decision (a choice or principle they settled on), \
relationship (who someone is to someone else), personal (job, home, health, family, \
background), other. \
at_seconds: the bracketed number of the supporting line. Return an empty list if there \
are none.
Decisions, priorities and principles someone states about how they want to work or live \
are facts about that person (the subject is the speaker, "{owner}" for the user), \
phrased as a decision, e.g. the user saying "I should stop prioritizing side projects \
before the base system is stable" -> {{"subject": "{owner}", "fact": "The owner decided \
to stabilise the base system before side projects."}}. A plan with no time is a fact \
too when it will still matter in a month (a trip, a purchase, a project, a move): "The \
owner plans to ...". Fewer good facts are better than many weak ones. Keys: \
facts[{{subject, fact, kind, at_seconds}}]."""

# The second look at the facts (``Summarizer._review_facts``): small models list too many.
REVIEW_SYSTEM = """You check facts picked from one conversation before they go into the \
user's long-term memory. Score each fact from 1 to 5:
5: certainly worth recalling in three months: an allergy, a job, a family member, a \
firm decision, a dated plan, a lasting preference;
4: likely still useful in three months;
3: true but minor or short-lived;
2: thinking aloud (considering, might), a request or task for someone, how a task was \
done or which tool was used, details of what was being worked on that day;
1: trivial, or vague (says "the device" or "the project" without naming it). A fact \
about a named project, place or thing is fine: score it like any other.
"said" is the transcript line the fact came from. Score every fact by its number. \
Output JSON only: {"scores": [{"number": <number>, "score": <1-5>}]}."""

LINK_SYSTEM = """You keep the user's memory of facts up to date. You get facts already \
known (with id and the date they were learned) and new facts from a conversation on \
{date}. For every new fact (by its number) say:
- "same": it says the same thing as a known fact (give its id);
- "updates": it is about the same thing as a known fact but the value changed or \
  conflicts, e.g. a new amount, date, place or decision (give its id);
- "new": anything else.
Only link facts about the same subject and the same thing. Output JSON only: \
{{"links": [{{"new": <number>, "relation": "...", "existing": <id or null>}}]}}."""

# Voice notes: the user talking to themselves (an idea, a reminder). Every line is the
# user's; no triage, never discarded.
NOTE_SYSTEM = """You write up a voice note the user recorded for themselves, for their \
private memory. """ + COMMON + """
Every line is the user talking to themselves: an idea, a plan or a reminder.
title: short and specific (what the note is about). summary: the idea in one or two \
sentences. key_points: details worth keeping (may be empty). people: names of people \
mentioned (not "{owner}"). topics: a few keywords. \
Keys: title, summary, key_points, people, topics."""

NOTE_ACTIONS_SYSTEM = """You list the to-dos in a voice note the user recorded for \
themselves. """ + COMMON + """
The note was recorded on {date}.
action_items: things the note says the user wants or needs to do ("call the plumber", \
"remember to buy milk", "email Bob about the invoice"), appointments and meetings it \
mentions, and things to be reminded of.
""" + ITEM_RULES + """
text: imperative and \
self-contained ("Call the plumber about the leaking tap"); an event: its name, see \
kind. owner: "{owner}" unless the \
note says someone else should do it. due: the deadline as said ("Friday"); due_date: \
that deadline as YYYY-MM-DD counted from the note's date, or null if there is none. \
at_seconds: the bracketed number of the line it comes from. Return an empty list if \
there are none.
""" + KINDS + """
kind "note" ONLY when the user explicitly asks to save a note: "create a note ...", "make \
a note ...", "take a note ...", "note that ...", "add to my hardware notes ...". text: what \
the note says, cleaned up, without the command words, e.g. "create a note battery \
dimensions are 13mm x 14mm x 3mm" -> text "Battery dimensions are 13 mm x 14 mm x 3 mm", \
title "Battery dimensions". list_name: the notes the user names to add it to, e.g. "add to \
my hardware notes: the screen is 1.4 inch" -> text "The screen is 1.4 inch", title \
"Screen size", list_name "hardware"; else null. A note has no due, time or owner. A plain \
statement without such a command ("the wifi password is on the router") is no note, and \
"remember to ..." is a task, not a note.
An appointment said without a verb is still an item: "meeting with the design team \
Tuesday 11 a.m." -> text "Meeting with the design team", kind event, due "Tuesday", time "11 a.m.".
""" + ACTION_KEYS

MERGE_SYSTEM = """You merge partial notes of ONE long conversation (given in order) into \
a single set of notes with the same JSON shape. Deduplicate. Output JSON only."""

FORGET_SYSTEM = """You edit the notes of one conversation because the user asked to \
forget some facts. Remove every statement of the facts to forget, and anything that \
reveals them, from the title, the summary, the key points and the topics. Keep everything \
else word for word. Never add anything. Drop a key point or topic that is only about a \
fact to forget. If the title gives one away, write a neutral title about what else was \
discussed. Output JSON only."""

DIGEST_SYSTEM = """You write a short daily digest of the user's conversations for {date} \
from their structured notes. Only use the given notes. Output JSON only."""


def format_transcript(segments: list[Segment], origin: float) -> str:
    lines = []
    for s in segments:
        off = max(0, int(s.started_at - origin))
        # a meeting's chat message: "[73] Dana (chat): see budget-v3.xlsx"
        who = f"{s.speaker} (chat)" if s.kind == "chat" else s.speaker
        lines.append(f"[{off}] {who}: {s.text}")
    return "\n".join(lines)
