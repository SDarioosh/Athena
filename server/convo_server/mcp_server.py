"""MCP server over the convo-ai store: the knowledge layer for any frontend.

Frontends (Hermes Agent, Odysseus, ...) connect over stdio (``convo-server mcp``) or
Streamable HTTP with a bearer token (``convo-server mcp --http``). The SQLite store stays
the source of truth; tools return compact text with a link to the transcript for every
claim so the frontend's model can cite sources.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from .config import Settings
from .diarize import load_voices
from .links import local_time, transcript_link
from .search import Embedder, search
from .speakers import relabel_matches, suggestion
from .store import CONVERSATION, ITEM_KINDS, NOTE, UNSET, Store

INSTRUCTIONS = """These tools search the user's own recorded conversations (transcripts \
with speaker names and timestamps, summaries, action items, facts, daily digests). Use them \
for any question about what someone said, decided, promised or mentioned, and before \
answering from memory. Questions about the user's plans, appointments and the people they \
know ("What is Jonas allergic to?") are answered from these recordings too: search \
(list_facts, search_transcripts) before saying you don't know, and search instead of offering \
to. Ground every claim in tool results and include the link given with it. Dates are in the \
user's timezone; pass dates as YYYY-MM-DD.

Naming speakers: when the user says who an unnamed speaker was ("Speaker 2 was Jenn", \
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
latest ones)."""

Text = Annotated[str, Field(description="Free-text words to look for; empty = no text filter")]
Person = Annotated[str, Field(description="Person's name as it appears in transcripts")]
Since = Annotated[str, Field(description="Start date YYYY-MM-DD (inclusive) or ISO time")]
Until = Annotated[str, Field(description="End date YYYY-MM-DD (inclusive) or ISO time")]
RecKind = Annotated[str, Field(description=(
    "'note' = only voice notes (memos the user recorded for themselves); "
    "'conversation' = only conversations with other people; empty = both"))]
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
    mcp = MCPServer("convo-ai", instructions=INSTRUCTIONS, version="0.3.0")
    ro = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    rw = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                         open_world_hint=False)
    # merging two names can't be undone (the lines of both look the same afterwards)
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
        a, b = window(since, until)
        rows = store.search_segments(query=query or None, speaker=speaker or None,
                                     since=a, until=b, limit=max(1, min(limit, 200)))
        if not rows and speaker:
            known = ", ".join(r["name"] for r in store.speaker_stats()[:20])
            return f"No lines by {speaker!r} in that range. Known speakers: {known or 'none'}."
        lines = [header(len(rows), "transcript lines")]
        for r in rows:
            link = transcript_link(settings, r["conversation_id"],
                                   r["started_at"] - r["conversation_at"])
            lines.append(f"- #{r['id']} {when(r['started_at'])} {r['speaker']}: \"{r['text']}\" "
                         f"(conversation \"{r['conversation_title'] or 'untitled'}\""
                         f"{_note(r.get('conversation_kind'))}, "
                         f"id {r['conversation_id']}) {link}")
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=ro)
    def search_conversations(query: Text = "", person: Person = "", since: Since = "",
                             until: Until = "", kind: RecKind = "", limit: int = 10) -> str:
        """Find conversations and voice notes by topic words, a person who spoke or was
        mentioned, and/or a date range, newest first. For the user's voice notes (memos,
        notes to self) set kind="note". Returns titles, summaries and links; use
        get_conversation for details."""
        a, b = window(since, until)
        limit = max(1, min(limit, 50))
        try:
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
        lines = [header(len(convs), "voice notes" if k == NOTE else "conversations")]
        for c in convs:
            lines.append(
                f"- {when(c['started_at'])}–{when(c['ended_at'], '%H:%M')} "
                f"\"{_title(c)}\"{_note(c.get('kind'))} id {c['id']}; people: "
                f"{', '.join(c['people']) or '—'}\n  {c['summary'] or ''}\n  "
                f"{transcript_link(settings, c['id'])}")
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

    @mcp.tool(annotations=ro)
    def get_conversation(conversation_id: ConvId) -> str:
        """Summary, key points, action items and facts of one conversation, with links."""
        c = store.get_conversation(conversation_id)
        if c is None:
            return f"No conversation {conversation_id!r}."
        speakers = sorted({s.speaker for s in store.conversation_segments(conversation_id)})
        out = [f"\"{_title(c)}\"{_note(c.get('kind'))} — {when(c['started_at'])}–"
               f"{when(c['ended_at'], '%H:%M')} ({settings.timezone}), device {c['device']}"
               + (f", type {c['kind']}" if c.get("kind") else ""),
               f"Speakers: {', '.join(speakers) or '—'}. People: {', '.join(c['people']) or '—'}",
               f"Link: {transcript_link(settings, c['id'])}",
               f"Summary: {c['summary'] or '(pending)'}"]
        out += [f"* {k}" for k in c["key_points"]]
        out += [_item(a, settings, c) for a in c["action_items"]]
        out += [f"Fact [{f['subject']}]: {f['fact']} " + _src(f, settings, c)
                for f in c["facts"]]
        out.append(f"{cite}.")
        return "\n".join(out)

    @mcp.tool(annotations=ro)
    def get_transcript(conversation_id: ConvId, start_seconds: float = 0,
                       end_seconds: float = -1, max_lines: int = 200) -> str:
        """Transcript lines of a conversation. start/end are seconds from its start
        (end -1 = to the end). Each line starts with its #number (for reassign_line)."""
        c = store.get_conversation(conversation_id)
        if c is None:
            return f"No conversation {conversation_id!r}."
        origin = c["started_at"]
        segs = [s for s in store.conversation_segments(conversation_id)
                if s.started_at - origin >= start_seconds
                and (end_seconds < 0 or s.started_at - origin <= end_seconds)]
        more = len(segs) > max_lines
        link = transcript_link(settings, c["id"])
        out = [f"\"{c['title'] or 'untitled'}\" {when(origin)} {link}"]
        out += [f"#{s.id} [{int(s.started_at - origin)}s {when(s.started_at, '%H:%M:%S')}] "
                f"{s.speaker}: {s.text}" for s in segs[:max_lines]]
        if more:
            out.append(f"… {len(segs) - max_lines} more lines; call again with a later "
                       "start_seconds.")
        out.append(f"{cite}.")
        return "\n".join(out)

    def item_link(r: dict[str, Any]) -> str:
        return transcript_link(settings, r["conversation_id"],
                               (r["source_at"] or r["conversation_at"]) - r["conversation_at"])

    def item_json(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "status": r["status"], "kind": r.get("kind") or "task",
                "text": r["text"], "owner": r["owner"], "due": r["due"],
                "due_date": r["due_date"], "due_time": r.get("due_time"),
                "duration_minutes": r.get("duration_minutes"), "location": r.get("location"),
                "time_unclear": bool(r.get("time_unclear")),
                "conversation_id": r["conversation_id"],
                "conversation_title": r["conversation_title"],
                "conversation_kind": r.get("conversation_kind"),
                "conversation_at": _iso(r["conversation_at"]),
                "created_at": _iso(r.get("created_at") or r["conversation_at"]),
                "timezone": settings.timezone, "link": item_link(r)}

    @mcp.tool(annotations=ro)
    def list_action_items(status: Annotated[str, Field(description="open, done, dismissed or all")]
                          = "open", person: Person = "", since: Since = "",
                          until: Until = "", format: Annotated[str, Field(  # noqa: A002
                              description="'text' (default) or 'json' (a JSON array, for "
                                          "programs)")] = "text") -> str:
        """Action items from conversations and voice notes (default: open ones), newest
        first. Each is a task, an event (meeting, appointment) or a reminder, with its due
        date and time of day if known."""
        fmt = (format or "text").strip().lower()
        if fmt not in ("text", "json"):
            return "format must be text or json"
        a, b = window(since, until)
        rows = store.list_action_items(None if status == "all" else status)
        rows = [r for r in rows
                if (a is None or r["conversation_at"] >= a)
                and (b is None or r["conversation_at"] < b)
                and (not person or person.lower() in (r["owner"] or "").lower())]
        if fmt == "json":
            return json.dumps([item_json(r) for r in rows], ensure_ascii=False)
        lines = [header(len(rows), f"{status} action items")]
        for r in rows:
            lines.append(f"- #{r['id']} [{r['status']}] {_kind_word(r)}{r['text']}"
                         + (f" (owner {r['owner']})" if r["owner"] else "")
                         + _due(r)
                         + f" — from \"{r['conversation_title']}\""
                         + f"{_note(r.get('conversation_kind'))} {when(r['conversation_at'])} "
                         + item_link(r))
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
                                                       "clear; empty = unchanged")] = "") -> str:
        """Change an action item: mark it done/dismissed (or reopen it), or change its kind,
        due date or time of day. Only when the user asks."""
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
                                            due_date=opt(due_date), due_time=opt(due_time))
        except ValueError as e:  # returned, not raised: the model sees the message
            return str(e)
        if item is None:
            return f"No action item #{item_id}."
        when_due = " ".join(x for x in (item["due_date"], item.get("due_time")) if x)
        return (f"Action item #{item_id} is now {item['status']}, {item['kind']}, "
                + (f"due {when_due}." if when_due else "no due date."))

    @mcp.tool(annotations=ro)
    def list_facts(subject: Annotated[str, Field(description="Person/thing the fact is about")]
                   = "", query: Text = "", limit: int = 50) -> str:
        """Durable facts learned from conversations (preferences, plans, relationships...).
        Only the latest version of each fact; older values it replaced are listed under it."""
        rows = store.list_facts(subject=subject or None, query=query or None,
                                limit=max(1, min(limit, 200)))
        lines = [header(len(rows), "facts")]
        for f in rows:
            off = (f["source_at"] or f["conversation_at"]) - f["conversation_at"]
            lines.append(f"- [{f['subject']}] {f['fact']} (learned "
                         f"{when(f['conversation_at'], '%Y-%m-%d')}) "
                         + transcript_link(settings, f["conversation_id"], off))
            for s in store.fact_sources(f["id"]):
                lines.append(f"  said again {when(s['conversation_at'], '%Y-%m-%d')} "
                             + transcript_link(settings, s["conversation_id"],
                                               (s["source_at"] or s["conversation_at"])
                                               - s["conversation_at"]))
            for old in store.fact_history(f["id"]):
                lines.append(f"  replaces: {old['fact']} (from "
                             f"{when(old['conversation_at'], '%Y-%m-%d')}) "
                             + transcript_link(settings, old["conversation_id"],
                                               (old["source_at"] or old["conversation_at"])
                                               - old["conversation_at"]))
        return "\n".join(lines + [f"{cite}."] * (len(lines) > 1))

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
            what = "Voice note" if conv and conv.get("kind") == "note" else "Conversation"
            out.append(f"{what}: {transcript_link(settings, cid)}")
        out.append(f"{cite}.")
        return "\n".join(out)

    @mcp.tool(annotations=ro)
    def list_people() -> str:
        """Speakers heard in transcripts (named, enrolled, and unnamed 'Speaker N' labels)
        and people mentioned in conversations."""
        enrolled = {s["name"] for s in store.speakers()}
        spk = store.speaker_stats()
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
        ("it's Jenn, not Jen"). If new_name already exists the two become one person.
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
        n, convs = store.rename_everywhere(old_name, new_name)
        if not n:
            return f"No lines by {old_name!r}. Known speakers: {_known(store)}."
        return (f"Renamed {old_name} to {new_name} in {n} lines across {len(convs)} "
                "conversations; their summaries will be redone.")

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


def _title(c: dict[str, Any]) -> str:
    if c.get("status") == "discarded":
        return f"(not summarized: looked like {c.get('discard_reason') or 'junk'})"
    return c.get("title") or "untitled (summary pending)"


def _kind(value: str) -> str | None:
    """search_conversations' ``kind``, read loosely: small models write "voice_note",
    "memo" or "notes"."""
    v = (value or "").strip().lower()
    if not v or v in ("all", "any", "both"):
        return None
    if "note" in v or "memo" in v:
        return NOTE
    if "conv" in v:
        return CONVERSATION
    raise ValueError(f"bad kind {value!r}; use 'note', 'conversation' or ''")


def _note(kind: str | None) -> str:
    """Marks a voice note (the owner talking to themselves) next to its title."""
    return " (voice note)" if kind == "note" else ""


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


def run(settings: Settings, store: Store, embedder: Embedder | None, http: bool) -> None:
    mcp = build_mcp(store, settings, embedder)
    if not http:
        mcp.run("stdio")
        return
    if not settings.mcp_token or len(settings.mcp_token) < 16:
        raise SystemExit("set CONVO_MCP_TOKEN (>= 16 chars) to serve MCP over HTTP")
    import uvicorn

    app = BearerAuth(mcp.streamable_http_app(host=settings.mcp_host), settings.mcp_token)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
