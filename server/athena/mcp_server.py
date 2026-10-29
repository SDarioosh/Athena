"""MCP server over the Athena store: the knowledge layer for any frontend.

Frontends (Hermes Agent, Odysseus, ...) connect over stdio (``convo-server mcp``) or
Streamable HTTP with a bearer token (``convo-server mcp --http``). The SQLite store stays
the source of truth; tools return compact text with a link to the transcript for every
claim so the frontend's model can cite sources. Over HTTP the same token can also upload
recordings (``/ingest/chunk``) and written notes (``/ingest/text``, see
``ingest_relay.py``); those routes are not tools.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from .config import Settings
from .dates import fact_valid_until
from .db.store import Store
from .diarize import load_voices
from .domain import (
    CONVERSATION,
    IN_PERSON,
    ITEM_KINDS,
    MEETING,
    NOTE,
    UNSET,
    VOICE,
    WRITTEN,
    short_title,
)
from .links import local_time, meeting_label, speaker_label, transcript_link, written_label
from .search import Embedder, search
from .speakers import relabel_matches, suggestion

INSTRUCTIONS = """These tools search the user's own recorded conversations (transcripts \
with speaker names and timestamps, summaries, action items, facts, daily digests). Use them \
for any question about what someone said, decided, promised or mentioned, and before \
answering from memory. Questions about the user's plans, appointments and the people they \
know ("What is Jonas allergic to?") are answered from these recordings too: search \
(list_facts, search_transcripts) before saying you don't know, and search instead of offering \
to. Ground every claim in tool results and include the link given with it. Dates are in the \
user's timezone; pass dates as YYYY-MM-DD.

Finding a conversation: when the user points at a conversation by its topic ("the one \
about AI glasses", "when we talked about the lease"), call search_conversations once with \
just the topic words (query="AI glasses"); it matches titles and summaries as well as what \
was said. Its results already show each conversation's title and summary: pick the match \
from them and answer with its link. Call get_conversation only for key points, action items \
or facts, and get_transcript only for exact wording the summary doesn't have. If nothing \
matches, try other words for the topic once, then search_transcripts. Don't call a tool \
again with the same arguments: you already have that result.

Naming speakers: when the user says who an unnamed speaker was ("Speaker 2 was Kath", \
"Speaker 1 and Speaker 3 were both Sam"), that is a request to save the name, not a claim \
to check. Find the conversation (search_conversations by date), call name_speaker once for \
each label, then say which labels were renamed (it also renames that voice in other \
conversations when it is sure). Fixing speakers: a name that is wrong or misspelled \
everywhere -> rename_speaker_everywhere; one line given to the wrong person -> \
reassign_line with the line's # number from get_transcript or search_transcripts. \
list_unnamed_speakers shows voices nobody has named yet, with quotes and a guess.

Voice notes: recordings the user made for themselves (an idea, a reminder) are voice \
notes, marked "(voice note)" after the title. When the user asks about their voice notes, \
notes or memos, call search_conversations with kind="note" (no query needed for the \
latest ones). Notes the user wrote by hand are notes too, marked "(written by hand: \
<link>)" instead; that link opens the handwritten page. kind="voice" keeps only voice \
notes, kind="written" only handwritten ones.

Online meetings: a meeting is marked "(Online meeting: <topic>)" after its title and lists \
its participants (everyone in the meeting, also those who never spoke). A line "Dana \
(chat): ..." was typed in the meeting chat, not said. For "who was in the X meeting" call \
search_conversations with the topic words and answer from its participants. To list \
the user's online meetings, call search_conversations with kind="meeting".

Showing a transcript: when the user asks to see or read a transcript, answer with its link \
and a short summary of who said what; don't copy the transcript lines into your answer \
(the link opens the full transcript). Quote only the lines the user asks about."""

Text = Annotated[str, Field(description="Free-text words to look for; empty = no text filter")]
Person = Annotated[str, Field(description="Person's name as it appears in transcripts")]
Since = Annotated[str, Field(description="Start date YYYY-MM-DD (inclusive) or ISO time")]
Until = Annotated[str, Field(description="End date YYYY-MM-DD (inclusive) or ISO time")]
RecKind = Annotated[str, Field(description=(
    "'note' = all the user's notes (voice notes and notes written by hand); "
    "'voice' = only voice notes (memos the user recorded for themselves); "
    "'written' = only notes the user wrote by hand; "
    "'conversation' = only conversations with other people (in person and online); "
    "'meeting' = only online meetings; 'in_person' = only conversations recorded in "
    "person (no online meetings); empty = everything"))]
ConvId = Annotated[str, Field(description="conversation id from a previous result")]


def parse_when(value: str, tz: ZoneInfo, *, end: bool = False) -> float | None:
    """'' -> None; 'YYYY-MM-DD' -> local midnight (``end``: midnight after); ISO -> as given."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
        return (day + timedelta(days=1) if end else day).timestamp()
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"bad date {value!r}; use YYYY-MM-DD") from e
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()


def build_mcp(store: Store, settings: Settings, embedder: Embedder | None = None):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tz = ZoneInfo(settings.timezone)
    mcp = MCPServer("athena", instructions=INSTRUCTIONS, version="0.20.1")
    ro = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    rw = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                         open_world_hint=False)
    # merging two names can't be undone (the lines of both look the same afterwards);
    # retire_fact uses it too: a forgotten fact is out of every listing for good; and
    # erase_fact: the words are gone from the transcript and the notes
    rw_merge = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                               idempotent_hint=True, open_world_hint=False)

    def when(ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return local_time(settings, ts, fmt) or "?"

    def window(since: str, until: str) -> tuple[float | None, float | None]:
        return parse_when(since, tz), parse_when(until, tz, end=True)

    # Frontends don't reliably forward server instructions (Hermes ignores them), so
    # the citation rule also rides along with every non-empty result: in the header and
    # again as the last line, the part a model reads right before it answers.
    cite = "When answering, put the link next to each fact you use"

    def header(n: int, what: str) -> str:
        if not n:
            return f"No {what} found."
        return f"{n} {what} (times are {settings.timezone}). {cite}:"

    @mcp.tool(annotations=ro)
    def search_transcripts(speaker: Person = "", query: Text = "", since: Since = "",
                           until: Until = "", limit: int = 30) -> str:
        """Find what was actually said: transcript lines filtered by speaker and date range,
        optionally matching words. Use for "what did X say (about Y) (on/last DATE)".
        Without a query, lines are returned in time order."""
        try:
            a, b = window(since, until)
        except ValueError as e:  # returned, not raised: the model sees the message
            return str(e)
        rows = store.search_segments(query=query or None, speaker=speaker or None,
                                     since=a, until=b, limit=max(1, min(limit, 200)))
        if not rows and speaker:
            known = ", ".join(r["name"] for r in store.speaker_stats()[:20])
            return f"No lines by {speaker!r} in that range. Known speakers: {known or 'none'}."
        lines = [header(len(rows), "transcript lines")]
        written = store.written_notes_for(r["conversation_id"] for r in rows)
        meetings = store.meetings_for(r["conversation_id"] for r in rows)
        for r in rows:
            link = transcript_link(settings, r["conversation_id"],
                                   r["started_at"] - r["conversation_at"])
            lines.append(f"- #{r['id']} {when(r['started_at'])} {speaker_label(r)}: "
                         f"\"{r['text']}\" "
                         f"(conversation \"{r['conversation_title'] or 'untitled'}\""
                         f"{_note(r.get('conversation_kind'), written.get(r['conversation_id']))}"
                         f"{meeting_label(meetings.get(r['conversation_id']))}, "
                         f"id {r['conversation_id']}) {link}")
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=ro)
    def search_conversations(query: Text = "", person: Person = "", since: Since = "",
                             until: Until = "", kind: RecKind = "", limit: int = 10) -> str:
        """Find conversations and voice notes by topic words, a person who spoke or was
        mentioned, and/or a date range (best match first with a query, else newest first).
        The first step for "the conversation about X": pass only the topic words; titles
        and summaries are searched as well as what was said. For the user's voice notes
        (memos, notes to self) set kind="note". Returns titles, summaries and links, often
        enough to answer; use get_conversation for details."""
        limit = max(1, min(limit, 50))
        try:
            a, b = window(since, until)
            k = _kind(kind)
        except ValueError as e:  # returned, not raised: the model sees the message
            return str(e)
        if query:
            hits = search(store, query, embedder, limit=limit * 5)
            allowed = None
            if person or k or a is not None or b is not None:
                allowed = {c["id"] for c in store.list_conversations(
                    since=a, until=b, person=person or None, kind=k, limit=10_000)}
            convs = [store.get_conversation(h["conversation_id"]) for h in hits
                     if allowed is None or h["conversation_id"] in allowed][:limit]
        else:
            convs = store.list_conversations(since=a, until=b, person=person or None,
                                             kind=k, limit=limit)
        convs = [c for c in convs if c]
        lines = [header(len(convs), _KIND_HEADER.get(k, "conversations"))]
        for c in convs:
            lines.append(
                f"- {when(c['started_at'])}–{when(c['ended_at'], '%H:%M')} "
                f"\"{_title(c)}\"{_note(c.get('kind'), c.get('written'))}"
                f"{meeting_label(c.get('meeting'))} id {c['id']}; people: "
                f"{', '.join(c['people']) or '—'}{_participants(c.get('meeting'))}\n  "
                f"{c['summary'] or ''}\n  {transcript_link(settings, c['id'])}")
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=ro)
    def get_conversation(conversation_id: ConvId) -> str:
        """Summary, key points, action items and facts of one conversation, with links."""
        c = store.get_conversation(conversation_id)
        if c is None:
            return f"No conversation {conversation_id!r}."
        speakers = sorted({s.speaker for s in store.conversation_segments(conversation_id)})
        out = [f"\"{_title(c)}\"{_note(c.get('kind'), c.get('written'))}"
               f"{meeting_label(c.get('meeting'))} — {when(c['started_at'])}–"
               f"{when(c['ended_at'], '%H:%M')} ({settings.timezone}), device {c['device']}"
               + (f", type {c['kind']}" if c.get("kind") else ""),
               f"Speakers: {', '.join(speakers) or '—'}. People: {', '.join(c['people']) or '—'}"
               + (f". Participants: {_attendance(c['meeting'], when)}" if c.get("meeting")
                  else ""),
               f"Link: {transcript_link(settings, c['id'])}",
               f"Summary: {_summary(c)}"]
        out += [f"* {k}" for k in c["key_points"]]
        out += [_item(a, settings, c) for a in c["action_items"]]
        out += [f"Fact [{f['subject']}]: {f['fact']} " + _src(f, settings, c)
                for f in c["facts"]]
        out.append(f"{cite}.")
        return "\n".join(out)

    @mcp.tool(annotations=ro)
    def get_transcript(conversation_id: ConvId, start_seconds: float = 0,
                       end_seconds: float = -1, max_lines: int = 100) -> str:
        """Transcript lines of a conversation, for exact wording. To find a conversation or
        say what it was about, use search_conversations / get_conversation instead.
        start/end are seconds from its start (end -1 = to the end). Each line starts with
        its #number (for reassign_line) and its [seconds from the start]. To show a
        transcript, give its link and a short summary; don't copy the lines into your
        answer."""
        c = store.get_conversation(conversation_id)
        if c is None:
            return f"No conversation {conversation_id!r}."
        origin = c["started_at"]
        segs = [s for s in store.conversation_segments(conversation_id)
                if s.started_at - origin >= start_seconds
                and (end_seconds < 0 or s.started_at - origin <= end_seconds)]
        more = len(segs) > max_lines
        link = transcript_link(settings, c["id"])
        # One time per line keeps the result short: a line's clock time is the header's
        # start time plus its seconds.
        out = [f"\"{c['title'] or 'untitled'}\" {when(origin, '%Y-%m-%d %H:%M:%S')} {link}"]
        out += [f"#{s.id} [{int(s.started_at - origin)}s] {speaker_label(s)}: {s.text}"
                for s in segs[:max_lines]]
        if pruned := c.get("transcript_pruned_at"):
            out.append(f"The transcript was removed on {when(pruned, '%Y-%m-%d')}"
                       " by the retention setting; use get_conversation for the summary, "
                       "facts and action items.")
        if more:
            out.append(f"… {len(segs) - max_lines} more lines; call again with a later "
                       "start_seconds.")
        out.append(f"{cite}. To show the transcript, give the link above and a short "
                   "summary; don't copy these lines into your answer.")
        return "\n".join(out)

    def item_link(r: dict[str, Any]) -> str:
        return transcript_link(settings, r["conversation_id"],
                               (r["source_at"] or r["conversation_at"]) - r["conversation_at"])

    def item_json(r: dict[str, Any], mine_names: set[str],
                  participants: dict[str, list[dict[str, Any]]],
                  written: dict[str, dict[str, Any]],
                  summaries: dict[str, dict[str, Any]],
                  meetings: dict[str, dict[str, Any]]) -> dict[str, Any]:
        owner = (r["owner"] or "").strip().lower()
        kind = r.get("kind") or "task"
        others = [] if kind == "note" else _with(r["text"], participants.get(
            r["conversation_id"], []))
        return {"id": r["id"], "status": r["status"], "kind": r.get("kind") or "task",
                "mine": not owner or owner in mine_names,
                "title": r.get("title") or short_title(r.get("note_title"), r["text"]),
                "explicit": bool(r.get("explicit")),
                "text": r["text"], "owner": r["owner"], "due": r["due"],
                "due_date": r["due_date"], "due_time": r.get("due_time"),
                "duration_minutes": r.get("duration_minutes"), "location": r.get("location"),
                "list_name": r.get("list_name"), "note_title": r.get("note_title"),
                "time_unclear": bool(r.get("time_unclear")),
                "conversation_id": r["conversation_id"],
                "conversation_title": r["conversation_title"],
                "conversation_kind": r.get("conversation_kind"),
                "conversation_at": _iso(r["conversation_at"]),
                "created_at": _iso(r.get("created_at") or r["conversation_at"]),
                "timezone": settings.timezone, "link": item_link(r), "with": others,
                "key_points": _note_key_points(r, summaries.get(r["conversation_id"])),
                "source": _source(r["conversation_id"], meetings),
                **_written_json(written.get(r["conversation_id"]))}

    @mcp.tool(annotations=ro)
    def list_action_items(status: Annotated[str, Field(description="open, done, dismissed or all")]
                          = "open", person: Person = "", since: Since = "",
                          until: Until = "", format: Annotated[str, Field(  # noqa: A002
                              description="'text' (default) or 'json' (a JSON array, for "
                                          "programs)")] = "text") -> str:
        """Action items from conversations and voice notes (default: open ones), newest
        first; person keeps those the person owns, is named in, or was in the conversation
        of. Each is a task, an event (meeting, appointment) or a reminder, with its due
        date and time of day if known, and the to-do list the user named for it. The JSON
        format also lists notes the user asked a voice note to save (kind "note", with
        note_title and, in list_name, the notes to add it to); the text format doesn't.
        Each JSON row has mine: false when someone else owns the item (the hub leaves
        those out of the user's to-dos), a short title and explicit: true when the user
        asked for the item (a date or time, a named list, a reminder, event or note, or a
        command such as "remind me" or "add a task"), false for one the summary only suggests.
        Each JSON row also has with: the other people in the item's conversation, those
        the item names first."""
        fmt = (format or "text").strip().lower()
        if fmt not in ("text", "json"):
            return "format must be text or json"
        st = (status or "open").strip().lower()
        if st not in ("open", "done", "dismissed", "all"):
            return "status must be open, done, dismissed or all"
        try:
            a, b = window(since, until)
        except ValueError as e:  # returned, not raised: the model sees the message
            return str(e)
        rows = store.list_action_items(None if st == "all" else st, notes=fmt == "json")
        rows = [r for r in rows
                if (a is None or r["conversation_at"] >= a)
                and (b is None or r["conversation_at"] < b)]
        participants = store.conversation_participants(
            (r["conversation_id"] for r in rows), _owner_names(settings)) \
            if fmt == "json" or person.strip() else {}
        if person.strip():
            rows = [r for r in rows if _involves(r, person, participants)]
        written = store.written_notes_for(r["conversation_id"] for r in rows)
        meetings = store.meetings_for(r["conversation_id"] for r in rows)
        if fmt == "json":
            mine_names = store.known_names()[0] | _OWNER_WORDS | {
                settings.owner_name.strip().lower()} - {""}
            summaries = store.summaries_for(
                r["conversation_id"] for r in rows if r.get("kind") == "note")
            return json.dumps([item_json(r, mine_names, participants, written, summaries,
                                         meetings) for r in rows], ensure_ascii=False)
        lines = [header(len(rows), f"{st} action items")]
        for r in rows:
            lines.append(f"- #{r['id']} [{r['status']}] {_kind_word(r)}{r['text']}"
                         + (f" (owner {r['owner']})" if r["owner"] else "")
                         + _due(r)
                         + (f" [list {r['list_name']}]" if r.get("list_name") else "")
                         + f" — from \"{r['conversation_title']}\""
                         + _note(r.get("conversation_kind"), written.get(r["conversation_id"]))
                         + meeting_label(meetings.get(r["conversation_id"]))
                         + f" {when(r['conversation_at'])} " + item_link(r))
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=rw)
    def update_action_item(
            item_id: int,
            status: Annotated[str, Field(description="open, done or dismissed; empty = "
                                                     "unchanged")] = "",
            kind: Annotated[str, Field(description="task, event or reminder; empty = "
                                                   "unchanged")] = "",
            due_date: Annotated[str, Field(description="YYYY-MM-DD, 'none' to clear; empty "
                                                       "= unchanged")] = "",
            due_time: Annotated[str, Field(description="HH:MM (24 h, local), 'none' to "
                                                       "clear; empty = unchanged")] = "",
            list_name: Annotated[str, Field(description="the to-do list it goes on, e.g. "
                                                        "'shopping'; 'none' to clear; empty "
                                                        "= unchanged")] = "",
            title: Annotated[str, Field(description="a short title, at most 60 characters; "
                                                    "'none' = from the text; empty = "
                                                    "unchanged")] = "") -> str:
        """Change an action item: mark it done/dismissed (or reopen it), or change its kind,
        due date, time of day, to-do list or title. Only when the user asks."""
        def opt(value: str) -> Any:
            v = (value or "").strip()
            return UNSET if not v else None if v.lower() == "none" else v

        st, kd = (status or "").strip().lower(), (kind or "").strip().lower()
        kd = "task" if kd in ("todo", "to-do") else kd
        if st and st not in {"open", "done", "dismissed"}:
            return "status must be open, done or dismissed"
        if kd and kd not in ITEM_KINDS:
            return "kind must be task, event or reminder"
        try:
            item = store.update_action_item(item_id, status=st or None, kind=kd or None,
                                            due_date=opt(due_date), due_time=opt(due_time),
                                            list_name=opt(list_name), title=opt(title))
        except ValueError as e:  # returned, not raised: the model sees the message
            return str(e)
        if item is None:
            return f"No action item #{item_id}."
        when_due = " ".join(x for x in (item["due_date"], item.get("due_time")) if x)
        return (f"Action item #{item_id} is now {item['status']}, {item['kind']}, "
                + (f"due {when_due}" if when_due else "no due date")
                + (f", on the {item['list_name']} list." if item.get("list_name") else "."))

    def fact_link(f: dict[str, Any]) -> str:
        return transcript_link(settings, f["conversation_id"],
                               (f["source_at"] or f["conversation_at"]) - f["conversation_at"])

    @mcp.tool(annotations=ro)
    def list_facts(subject: Annotated[str, Field(description="Person/thing the fact is about")]
                   = "", query: Text = "", limit: int = 50,
                   confirmed: Annotated[bool, Field(
                       description="true = only confirmed facts: said in several "
                                   "conversations on different days, not replaced since")]
                   = False,
                   format: Annotated[str, Field(  # noqa: A002
                       description="'text' (default) or 'json' (a JSON object, for "
                                   "programs)")] = "text") -> str:
        """Durable facts learned from conversations (preferences, plans, relationships...).
        Only the latest version of each fact; older values it replaced are listed under it."""
        fmt = (format or "text").strip().lower()
        if fmt not in ("text", "json"):
            return "format must be text or json"

        def until(f: dict[str, Any]) -> str | None:
            """The last day a fact about one occasion is worth keeping, or None."""
            said_on = datetime.fromtimestamp(f["conversation_at"], tz).date()
            end = fact_valid_until(f["fact"], said_on)
            return end.isoformat() if end else None

        counted = confirmed or fmt == "json"
        # programs reconcile by id, so the JSON cap is high enough to hold every fact
        cap = 1000 if fmt == "json" else 200
        rows = store.list_facts(
            subject=subject or None, query=query or None, limit=max(1, min(limit, cap)),
            counts_tz=settings.timezone if counted else None,
            min_sources=settings.fact_confirm_sources if confirmed else 0,
            min_days=settings.fact_confirm_days if confirmed else 0)
        written = store.written_notes_for(f["conversation_id"] for f in rows)
        meetings = store.meetings_for(f["conversation_id"] for f in rows)
        if fmt == "json":
            owners, people = store.known_names()
            owners |= _OWNER_WORDS | {settings.owner_name.strip().lower()} - {""}
            owner_id = store.owner_person_id(_owner_names(settings))
            quotes = store.fact_quotes(rows)

            def fact_json(f: dict[str, Any]) -> dict[str, Any]:
                about = _about(f["subject"], owners, people)
                pid = (owner_id if about == "owner" else
                       store.person_id(f["subject"] or "") if about == "person" else None)
                return {"id": f["id"], "subject": f["subject"] or "", "about": about,
                        "person_id": pid, "fact": f["fact"], "kind": f.get("kind"),
                        "conversation_id": f["conversation_id"],
                        "quote": quotes.get(f["id"]),
                        "times_said": f["times_said"], "days": f["days"],
                        "first_said": f["first_said"], "last_said": f["last_said"],
                        "confirmed": (f["times_said"] >= settings.fact_confirm_sources
                                      and f["days"] >= settings.fact_confirm_days),
                        "valid_until": until(f), "link": fact_link(f),
                        "source": _source(f["conversation_id"], meetings),
                        **_written_json(written.get(f["conversation_id"]))}

            return json.dumps({"facts": [fact_json(f) for f in rows]}, ensure_ascii=False)
        lines = [header(len(rows), "confirmed facts" if confirmed else "facts")]
        sources = {f["id"]: store.fact_sources(f["id"]) for f in rows}
        history = {f["id"]: store.fact_history(f["id"]) for f in rows}
        written.update(store.written_notes_for(
            r["conversation_id"] for rs in (*sources.values(), *history.values()) for r in rs))
        for f in rows:
            end = until(f)
            lines.append(f"- [{f['subject']}] {f['fact']} (learned "
                         f"{when(f['conversation_at'], '%Y-%m-%d')}"
                         + (f", until {end}" if end else "") + ") " + fact_link(f)
                         + written_label(written.get(f["conversation_id"]))
                         + meeting_label(meetings.get(f["conversation_id"])))
            for s in sources[f["id"]]:
                lines.append(f"  said again {when(s['conversation_at'], '%Y-%m-%d')} "
                             + transcript_link(settings, s["conversation_id"],
                                               (s["source_at"] or s["conversation_at"])
                                               - s["conversation_at"])
                             + written_label(written.get(s["conversation_id"])))
            for old in history[f["id"]]:
                lines.append(f"  replaces: {old['fact']} (from "
                             f"{when(old['conversation_at'], '%Y-%m-%d')}) "
                             + transcript_link(settings, old["conversation_id"],
                                               (old["source_at"] or old["conversation_at"])
                                               - old["conversation_at"])
                             + written_label(written.get(old["conversation_id"])))
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=rw_merge)
    def retire_fact(fact_id: Annotated[int, Field(description="The fact's id (list_facts "
                                                              "format=json)")],
                    reason: Annotated[str, Field(description="Why, e.g. 'wrong' or "
                                                             "'private'; optional")] = ""
                    ) -> str:
        """Forget a fact the user says is wrong or should not be kept. It disappears from
        list_facts and search, and stays forgotten when it is said again or its
        conversation is re-summarized. Only when the user asks."""
        result = store.retire_fact(fact_id, reason)
        if result is None:
            return f"No fact #{fact_id}."
        if result == "already":
            return f"Fact #{fact_id} was already retired."
        return f"Fact #{fact_id} retired."

    @mcp.tool(annotations=rw_merge)
    def erase_fact(fact_id: Annotated[int, Field(description="The fact's id (list_facts "
                                                             "format=json)")],
                   reason: Annotated[str, Field(description="Why, e.g. 'wrong' or "
                                                            "'private'; optional")] = ""
                   ) -> str:
        """Forget a fact completely: everything retire_fact does, and its words are erased
        where they were said. The transcript line that said it reads "[forgotten]" (in its
        conversation and in every later one that said it again), the conversation's
        summary is rewritten without it, and its digest is written again. Can't be undone.
        Only when the user asks."""
        result = store.erase_fact(fact_id, reason)
        if result is None:
            return f"No fact #{fact_id}."
        if result["result"] == "already":
            return f"Fact #{fact_id} was already erased."
        return (f"Fact #{fact_id} erased: {result['lines']} transcript line(s) redacted, "
                f"{result['conversations']} conversation summary(ies) being rewritten.")

    @mcp.tool(annotations=ro)
    def get_daily_digest(date: Annotated[str, Field(description="YYYY-MM-DD")]) -> str:
        """The daily digest for a date: headline, summary, highlights, open action items."""
        d = store.get_digest(date)
        if d is None:
            return f"No digest for {date}. Try search_conversations with since/until = {date}."
        out = [f"{date}: {d['headline']}", d["summary"]]
        out += [f"* {h}" for h in d.get("highlights", [])]
        out += [f"TODO: {t}" for t in d.get("open_action_items", [])]
        for cid in d.get("conversation_ids", []):
            conv = store.get_conversation(cid)
            what = ("Written note" if conv and conv.get("written") else
                    "Voice note" if conv and conv.get("kind") == "note" else
                    "Online meeting" if conv and conv.get("meeting") else "Conversation")
            out.append(f"{what}: {transcript_link(settings, cid)}")
        out.append(f"{cite}.")
        return "\n".join(out)

    @mcp.tool(annotations=ro)
    def list_people(format: Annotated[str, Field(  # noqa: A002
            description="'text' (default) or 'json' (a JSON object with each person's "
                        "stable id, for programs)")] = "text") -> str:
        """Speakers heard in transcripts (named, enrolled, and unnamed 'Speaker N' labels)
        and people mentioned in conversations."""
        fmt = (format or "text").strip().lower()
        if fmt not in ("text", "json"):
            return "format must be text or json"
        spk = store.speaker_stats()
        if fmt == "json":
            return json.dumps({"people": _people_json(store, spk, when, _owner_names(settings))},
                              ensure_ascii=False)
        enrolled = {s["name"] for s in store.speakers()}
        out = ["Speakers:"]
        out += [f"- {s['name']}{' (owner)' if s['is_owner'] else ''}"
                f"{' (voice known)' if s['name'] in enrolled else ''}: {s['lines']} lines in "
                f"{s['conversations']} conversations, last {when(s['last_heard'])}"
                for s in spk]
        out.append("Mentioned:")
        out += [f"- {p['name']}: {p['conversations']} conversations, last "
                f"{when(p['last_mentioned'], '%Y-%m-%d')}" for p in store.mentioned_people()]
        return "\n".join(out)

    @mcp.tool(annotations=rw)
    def name_speaker(conversation_id: ConvId,
                     speaker_label: Annotated[str, Field(description="e.g. 'Speaker 1'")],
                     name: Annotated[str, Field(description="The person's real name")],
                     remember_voice: bool = True) -> str:
        """Save who an unnamed speaker was: rename 'Speaker N' to a real name in one
        conversation. Call it whenever the user says who a speaker was ("Speaker 2 was
        Alice"); call it once per label (two labels may get the same name). The summary is
        redone with the name; with remember_voice, future recordings recognise them."""
        name = name.strip()
        if not name:
            return "name must not be empty"
        n, voice = store.rename_speaker(conversation_id, speaker_label, name)
        if not n:
            return f"No lines by {speaker_label!r} in conversation {conversation_id}."
        msg = f"Renamed {n} lines from {speaker_label} to {name}; the summary will be redone."
        if remember_voice:
            if voice is not None:
                store.add_voice_sample(name, voice)
                msg += f" {name}'s voice will be recognised in new recordings."
                msg += _relabel_note(store, name, settings)
            else:
                msg += " (No voice sample stored for that label, so it can't be remembered.)"
        return msg

    @mcp.tool(annotations=rw_merge)
    def rename_speaker_everywhere(
            old_name: Annotated[str, Field(description="Speaker name as it appears now")],
            new_name: Annotated[str, Field(description="The correct name")]) -> str:
        """Rename a speaker in ALL conversations, e.g. to fix a misspelled or wrong name
        ("it's Kath, not Kat"). If new_name already exists the two become one person.
        For an unnamed 'Speaker N' use name_speaker (those labels are per conversation)."""
        old_name, new_name = old_name.strip(), new_name.strip()
        if not old_name or not new_name:
            return "old_name and new_name must not be empty"
        if _UNNAMED.match(old_name):
            return (f"{old_name!r} is a per-conversation label; use name_speaker with the "
                    "conversation id instead.")
        if _UNNAMED.match(new_name):  # would merge a person into an unnamed label
            return (f"{new_name!r} is not a name; give the person's real name "
                    "(reassign_line moves single lines).")
        old_id, new_id = store.person_id(old_name), store.person_id(new_name)
        n, convs = store.rename_everywhere(old_name, new_name)
        if not n:
            return f"No lines by {old_name!r}. Known speakers: {_known(store)}."
        msg = (f"Renamed {old_name} to {new_name} in {n} lines across {len(convs)} "
               "conversations; their summaries will be redone.")
        pid = store.person_id(new_name)
        if pid is None:
            return msg
        if old_id is not None and new_id is not None and old_id != new_id:
            return msg + (f" {old_name} (person id {old_id}) is merged into {new_name}, "
                          f"whose person id {pid} is kept.")
        return msg + (f" The person id {pid} is kept." if old_id == pid
                      else f" Person id {pid}.")

    @mcp.tool(annotations=rw)
    def reassign_line(line_id: Annotated[int, Field(
                          description="The line's #number from get_transcript or "
                                      "search_transcripts")],
                      name: Annotated[str, Field(description="Who really said it")]) -> str:
        """Fix one transcript line that was given to the wrong speaker."""
        name = name.strip()
        if not name:
            return "name must not be empty"
        before = store.reassign_segment(line_id, name)
        if before is None:
            return f"No line #{line_id}."
        if before.speaker == name:
            return f"Line #{line_id} is already {name}'s."
        return (f"Line #{line_id} (\"{before.text}\") moved from {before.speaker} to {name}; "
                "the conversation's summary will be redone.")

    @mcp.tool(annotations=ro)
    def list_unnamed_speakers(limit: int = 10) -> str:
        """Voices nobody has named yet ('Speaker N'), newest first: a few of their lines,
        the conversation, and who they sound like if anyone known is close. Use to ask the
        user "who is this?"; save the answer with name_speaker."""
        cands = store.unnamed_candidates()[: max(1, min(limit, 50))]
        if not cands:
            return "No unnamed speakers found."
        voices = load_voices(store)
        out = [header(len(cands), "unnamed speakers")]
        for k in cands:
            conv = store.get_conversation(k["conversations"][0])
            if conv is None:
                continue
            lines = [s for s in store.conversation_segments(conv["id"])
                     if s.session_id == k["session_id"] and s.speaker == k["label"]]
            quotes = sorted(lines, key=lambda s: -len(s.text))[:3]
            guess = suggestion(voices, k["embedding"],
                               settings.speaker_match_threshold - 0.1) if voices else None
            out.append(
                f"- {k['label']} in \"{conv['title'] or 'untitled'}\" "
                f"{when(conv['started_at'])} (conversation id {conv['id']}), {k['lines']} "
                "lines" + (f"; sounds like {guess[0]} ({guess[1]:.2f})" if guess else ""))
            out += [f"  #{q.id} \"{q.text}\" "
                    + transcript_link(settings, conv["id"], q.started_at - conv["started_at"])
                    for q in sorted(quotes, key=lambda s: s.started_at)]
        return "\n".join(out)

    return mcp


# Subjects that mean the owner (the user) in a fact, besides the owner's own names.
_OWNER_WORD_ORDER = ("me", "i", "myself", "owner", "the owner", "user", "the user")
_OWNER_WORDS = set(_OWNER_WORD_ORDER)


def _about(subject: str | None, owners: set[str], people: set[str]) -> str:
    """Who a fact is about: "owner", "person" (a known speaker or a person named in a
    conversation) or "other"."""
    s = (subject or "").strip().lower()
    if s in owners:
        return "owner"
    return "person" if s and s in people else "other"


def _with(text: str, people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """list_action_items JSON ``with``: the conversation's other people, those whose name
    (any name they appear under) is a word in the item's text first, then the rest in
    order of first appearance."""
    def named(p: dict[str, Any]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text or "", re.IGNORECASE)
                   for n in p["names"])

    ranked = sorted(people, key=lambda p: not named(p))
    return [{"id": p["id"], "name": p["name"]} for p in ranked]


def _involves(row: dict[str, Any], person: str,
              participants: dict[str, list[dict[str, Any]]]) -> bool:
    """list_action_items(person=...): the person owns the item, is named in its text, or
    is one of the other people in its conversation (the item's ``with``)."""
    p = person.strip().lower()
    if p in (row["owner"] or "").lower():
        return True
    if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", row["text"] or "", re.IGNORECASE):
        return True
    return any(p in n.lower() for e in participants.get(row["conversation_id"], [])
               for n in e["names"])


def _owner_names(settings: Settings) -> tuple[str, ...]:
    """Names that mean the owner, in order: CONVO_OWNER_NAME, then the owner words."""
    names = [settings.owner_name.strip(), *_OWNER_WORD_ORDER]
    return tuple(dict.fromkeys(n for n in names if n))


def _people_json(store: Store, speaker_stats: list[dict[str, Any]], when: Any,
                 owner_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """list_people(format="json"): every person not merged into another, in the text
    mode's order (speakers by last heard, then people mentioned by last mention, then
    people never seen, by name). Unnamed 'Speaker N' labels have no person row."""
    rank: dict[int, int] = {}
    names = [s["name"] for s in speaker_stats] + [p["name"] for p in store.mentioned_people()]
    for name in names:
        pid = store.person_id(name, owner_names)
        if pid is not None:
            rank.setdefault(pid, len(rank))
    people = sorted(store.people(owner_names), key=lambda p: (rank.get(p["id"], len(rank)),
                                                   p["name"].lower()))
    return [{"id": p["id"], "name": p["name"], "aliases": p["aliases"],
             "is_owner": p["is_owner"], "has_voice": p["has_voice"],
             "conversations": p["conversations"],
             "first_seen": when(p["first_at"], "%Y-%m-%d") if p["first_at"] is not None else None,
             "last_seen": when(p["last_at"], "%Y-%m-%d") if p["last_at"] is not None else None}
            for p in people]


_UNNAMED = re.compile(r"^(speaker\s*\d+|unknown)$", re.IGNORECASE)


def _known(store: Store) -> str:
    return ", ".join(r["name"] for r in store.speaker_stats()[:20]) or "none"


def _relabel_note(store: Store, name: str, settings: Settings) -> str:
    done = relabel_matches(store, name, settings.speaker_relabel_threshold)
    if not done:
        return ""
    lines = sum(r.lines for r in done)
    convs = {c for r in done for c in r.conversations}
    return (f" Their voice also matched {len(done)} unnamed speaker(s) elsewhere, renamed "
            f"to {name} in {lines} lines across {len(convs)} other conversation(s).")


def _summary(c: dict[str, Any]) -> str:
    if c.get("forget_pending"):
        return "(being rewritten after the user forgot something; ask again later)"
    if c.get("transcript_pruned_at") and not c.get("summary"):
        return "(none; the transcript was removed by the retention setting)"
    return c["summary"] or "(pending)"


def _title(c: dict[str, Any]) -> str:
    if c.get("forget_pending"):
        return "untitled (being rewritten after a forget)"
    if c.get("status") == "discarded":
        return f"(not summarized: looked like {c.get('discard_reason') or 'junk'})"
    return c.get("title") or "untitled (summary pending)"


_KIND_HEADER = {NOTE: "notes", VOICE: "voice notes", WRITTEN: "written notes",
                MEETING: "online meetings"}


def _kind(value: str) -> str | None:
    """search_conversations' ``kind``, read loosely: small models write "voice_note",
    "memo", "handwritten" or "notes"; "zoom" or "online" for a meeting."""
    v = (value or "").strip().lower()
    if not v or v in ("all", "any", "both"):
        return None
    if "person" in v or "face" in v or "offline" in v:
        return IN_PERSON
    if "meet" in v or "zoom" in v or "online" in v:
        return MEETING
    if "writ" in v or "hand" in v:
        return WRITTEN
    if "voice" in v or "memo" in v or "spoken" in v:
        return VOICE
    if "note" in v:
        return NOTE
    if "conv" in v:
        return CONVERSATION
    raise ValueError(f"bad kind {value!r}; use 'note', 'voice', 'written', 'conversation', "
                     "'meeting', 'in_person' or ''")


def _note(kind: str | None, written: dict[str, Any] | None = None) -> str:
    """Marks a voice note (the owner talking to themselves) next to its title, or a note
    written by hand in the hub, with the link to it there."""
    if written:
        return written_label(written)
    return " (voice note)" if kind == "note" else ""


def _note_key_points(r: dict[str, Any], summary: dict[str, Any] | None) -> list[str]:
    """JSON rows' ``key_points`` (MCP 0.15.0): for a note that is a whole voice note (its
    text is the voice note's summary, see ``_lone_note`` in summarize/rules/notes.py), the
    voice note's key points, so the hub's note holds what was said ("Screen: 49 mm x
    60 mm"), not only the summary; ``[]`` for every other item."""
    if r.get("kind") != "note" or r.get("list_name") or not summary:
        return []
    said = " ".join((summary.get("summary") or "").split())
    if not said or said != " ".join((r.get("text") or "").split()):
        return []
    return [str(k).strip() for k in summary.get("key_points") or [] if str(k).strip()]


def _source(conv_id: str, meetings: dict[str, dict[str, Any]]) -> str | None:
    """JSON rows' ``source`` (MCP 0.16.0): "meeting" when the fact or item comes from a
    meeting (Zoom today), else null (a recording, a voice note or a written note; those are told
    apart by ``conversation_kind`` and ``written_by_hand``)."""
    return "meeting" if conv_id in meetings else None


def _participants(meeting: dict[str, Any] | None) -> str:
    """"; participants: A, B" after a meeting's people in search_conversations."""
    if not meeting or not meeting.get("participants"):
        return ""
    return "; participants: " + ", ".join(p["name"] for p in meeting["participants"])


def _attendance(meeting: dict[str, Any], when: Any) -> str:
    """get_conversation's participants: "Dana Example (16:00–16:45), Me", with the times
    they joined and left when the bot sent them."""
    out = []
    for p in meeting.get("participants") or []:
        span = "–".join(when(t, "%H:%M") for t in (p.get("joined_at"), p.get("left_at"))
                        if t is not None)
        out.append(p["name"] + (f" ({span})" if span else ""))
    return ", ".join(out) or "—"


def _written_json(written: dict[str, Any] | None) -> dict[str, Any]:
    """JSON rows' fields for a written note (MCP 0.12.0): ``written_by_hand`` and
    ``note_link``, the note in the hub (null for spoken ones)."""
    return {"written_by_hand": bool(written),
            "note_link": written["link"] if written else None}


def _due(a: dict[str, Any]) -> str:
    """" due 2026-10-21 12:00 (Wednesday at 12 pm)", plus " [time unclear]" when set."""
    when_due = " ".join(x for x in (a.get("due_date"), a.get("due_time")) if x)
    if when_due:
        out = f" due {when_due}" + (f" ({a['due']})" if a.get("due") else "")
    else:
        out = f" due {a['due']}" if a.get("due") else ""
    return out + (" [time unclear]" if a.get("time_unclear") else "")


def _kind_word(a: dict[str, Any]) -> str:
    """"event: " / "reminder: " before the text; nothing for a task."""
    kind = a.get("kind") or "task"
    return "" if kind == "task" else f"{kind}: "


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


def _src(item: dict[str, Any], settings: Settings, conv: dict[str, Any]) -> str:
    off = (item.get("source_at") or conv["started_at"]) - conv["started_at"]
    return transcript_link(settings, conv["id"], off)


def _item(a: dict[str, Any], settings: Settings, conv: dict[str, Any]) -> str:
    kind = _kind_word(a)
    return (f"Action #{a['id']} [{a['status']}]{' ' + kind if kind else ': '}{a['text']}"
            + (f" (owner {a['owner']})" if a.get("owner") else "")
            + _due(a) + " " + _src(a, settings, conv))


class BearerAuth:
    """ASGI middleware: require the MCP token on every HTTP request.

    Accepts ``Authorization: Bearer <token>`` (Hermes) or ``?access_token=<token>`` in the
    URL, because some clients (Odysseus) only support OAuth and can't send a static
    header. Keep the HTTP transport on loopback / the docker bridge / Tailscale.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.encode()

    def _ok(self, scope: dict) -> bool:
        headers = dict(scope.get("headers") or [])
        scheme, _, value = headers.get(b"authorization", b"").partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(value, self.token):
            return True
        from urllib.parse import parse_qs

        query = parse_qs((scope.get("query_string") or b"").decode())
        given = (query.get("access_token") or [""])[0].encode()
        return bool(given) and hmac.compare_digest(given, self.token)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not self._ok(scope):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


_ACCESS_TOKEN = re.compile(r"(access_token=)[^&\s\"]*")


class HideAccessToken(logging.Filter):
    """Masks ``?access_token=`` in uvicorn's access log lines (Odysseus sends the MCP
    token in the URL)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_ACCESS_TOKEN.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        record.msg = _ACCESS_TOKEN.sub(r"\1***", str(record.msg))
        return True


def _log_config() -> dict[str, Any]:
    import copy

    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["hide_access_token"] = {"()": HideAccessToken}
    config["handlers"]["access"]["filters"] = ["hide_access_token"]
    return config


def run(settings: Settings, store: Store, embedder: Embedder | None, http: bool) -> None:
    mcp = build_mcp(store, settings, embedder)
    if not http:
        mcp.run("stdio")
        return
    if not settings.mcp_token or len(settings.mcp_token) < 16:
        raise SystemExit("set CONVO_MCP_TOKEN (>= 16 chars) to serve MCP over HTTP")
    import uvicorn

    from .ingest_relay import add_ingest_routes

    # Plain HTTP upload routes next to /mcp (not tools), forwarded to the ingest API.
    add_ingest_routes(mcp, settings)
    app = BearerAuth(mcp.streamable_http_app(host=settings.mcp_host), settings.mcp_token)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_config=_log_config())
