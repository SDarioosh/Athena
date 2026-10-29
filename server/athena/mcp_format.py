"""The text the MCP tools answer with (``mcp_server.py``).

Every non-empty result asks for the link next to each fact used, in its header and again
as its last line: frontends don't reliably forward the server instructions (Hermes
ignores them), and the last line is what a model reads right before it answers. The
exact wording is part of the contract with the hub (``tests/contract/snapshots``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import Settings
from .domain import MEETING, NOTE, VOICE, WRITTEN
from .links import local_time, meeting_label, speaker_label, transcript_link, written_label

CITE = "When answering, put the link next to each fact you use"

KIND_HEADER = {NOTE: "notes", VOICE: "voice notes", WRITTEN: "written notes",
               MEETING: "online meetings"}


class Fmt:
    """The settings a result's times and links need."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def when(self, ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return local_time(self.settings, ts, fmt) or "?"

    def link(self, conv_id: str, offset: float | None = None) -> str:
        return transcript_link(self.settings, conv_id, offset)

    def said_at(self, r: dict[str, Any]) -> str:
        """The link to where a row (fact, action item, fact source; with
        ``conversation_id``, ``source_at`` and ``conversation_at``) was said."""
        return self.link(r["conversation_id"],
                         (r["source_at"] or r["conversation_at"]) - r["conversation_at"])

    def header(self, n: int, what: str) -> str:
        if not n:
            return f"No {what} found."
        return f"{n} {what} (times are {self.settings.timezone}). {CITE}:"


def cited(lines: list[str]) -> str:
    """A list result: its lines, and the citation rule again when there is any row."""
    return "\n".join(lines + [f"{CITE}."] * (len(lines) > 1))


# ------------------------------------------------------------------ tools
def transcript_lines(f: Fmt, rows: list[dict[str, Any]], written: dict[str, Any],
                     meetings: dict[str, Any]) -> str:
    """search_transcripts."""
    lines = [f.header(len(rows), "transcript lines")]
    for r in rows:
        link = f.link(r["conversation_id"], r["started_at"] - r["conversation_at"])
        lines.append(f"- #{r['id']} {f.when(r['started_at'])} {speaker_label(r)}: "
                     f"\"{r['text']}\" "
                     f"(conversation \"{r['conversation_title'] or 'untitled'}\""
                     f"{note(r.get('conversation_kind'), written.get(r['conversation_id']))}"
                     f"{meeting_label(meetings.get(r['conversation_id']))}, "
                     f"id {r['conversation_id']}) {link}")
    return cited(lines)


def conversations(f: Fmt, convs: list[dict[str, Any]], kind: str | None) -> str:
    """search_conversations."""
    lines = [f.header(len(convs), KIND_HEADER.get(kind or "", "conversations"))]
    for c in convs:
        lines.append(
            f"- {f.when(c['started_at'])}–{f.when(c['ended_at'], '%H:%M')} "
            f"\"{title(c)}\"{note(c.get('kind'), c.get('written'))}"
            f"{meeting_label(c.get('meeting'))} id {c['id']}; people: "
            f"{', '.join(c['people']) or '—'}{participants(c.get('meeting'))}\n  "
            f"{c['summary'] or ''}\n  {f.link(c['id'])}")
    return cited(lines)


def conversation(f: Fmt, c: dict[str, Any], speakers: list[str]) -> str:
    """get_conversation."""
    out = [f"\"{title(c)}\"{note(c.get('kind'), c.get('written'))}"
           f"{meeting_label(c.get('meeting'))} — {f.when(c['started_at'])}–"
           f"{f.when(c['ended_at'], '%H:%M')} ({f.settings.timezone}), device {c['device']}"
           + (f", type {c['kind']}" if c.get("kind") else ""),
           f"Speakers: {', '.join(speakers) or '—'}. People: {', '.join(c['people']) or '—'}"
           + (f". Participants: {attendance(c['meeting'], f.when)}" if c.get("meeting")
              else ""),
           f"Link: {f.link(c['id'])}",
           f"Summary: {summary(c)}"]
    out += [f"* {k}" for k in c["key_points"]]
    out += [_item(f, a, c) for a in c["action_items"]]
    out += [f"Fact [{x['subject']}]: {x['fact']} " + _src(f, x, c) for x in c["facts"]]
    out.append(f"{CITE}.")
    return "\n".join(out)


def transcript(f: Fmt, c: dict[str, Any], segs: list[Any], max_lines: int) -> str:
    """get_transcript: one time per line keeps the result short (a line's clock time is
    the header's start time plus its seconds)."""
    origin = c["started_at"]
    out = [f"\"{c['title'] or 'untitled'}\" {f.when(origin, '%Y-%m-%d %H:%M:%S')} "
           f"{f.link(c['id'])}"]
    out += [f"#{s.id} [{int(s.started_at - origin)}s] {speaker_label(s)}: {s.text}"
            for s in segs[:max_lines]]
    if pruned := c.get("transcript_pruned_at"):
        out.append(f"The transcript was removed on {f.when(pruned, '%Y-%m-%d')}"
                   " by the retention setting; use get_conversation for the summary, "
                   "facts and action items.")
    if len(segs) > max_lines:
        out.append(f"… {len(segs) - max_lines} more lines; call again with a later "
                   "start_seconds.")
    out.append(f"{CITE}. To show the transcript, give the link above and a short "
               "summary; don't copy these lines into your answer.")
    return "\n".join(out)


def action_items(f: Fmt, rows: list[dict[str, Any]], status: str,
                 written: dict[str, Any], meetings: dict[str, Any]) -> str:
    """list_action_items, text format."""
    lines = [f.header(len(rows), f"{status} action items")]
    for r in rows:
        lines.append(f"- #{r['id']} [{r['status']}] {kind_word(r)}{r['text']}"
                     + (f" (owner {r['owner']})" if r["owner"] else "")
                     + due(r)
                     + (f" [list {r['list_name']}]" if r.get("list_name") else "")
                     + f" — from \"{r['conversation_title']}\""
                     + note(r.get("conversation_kind"), written.get(r["conversation_id"]))
                     + meeting_label(meetings.get(r["conversation_id"]))
                     + f" {f.when(r['conversation_at'])} " + f.said_at(r))
    return cited(lines)


def updated_item(item_id: int, item: dict[str, Any]) -> str:
    """update_action_item."""
    when_due = " ".join(x for x in (item["due_date"], item.get("due_time")) if x)
    return (f"Action item #{item_id} is now {item['status']}, {item['kind']}, "
            + (f"due {when_due}" if when_due else "no due date")
            + (f", on the {item['list_name']} list." if item.get("list_name") else "."))


def facts(f: Fmt, rows: list[dict[str, Any]], confirmed: bool,
          sources: dict[int, list[dict[str, Any]]], history: dict[int, list[dict[str, Any]]],
          written: dict[str, Any], meetings: dict[str, Any],
          until: Callable[[dict[str, Any]], str | None]) -> str:
    """list_facts, text format: each fact, where it was said again and what it
    replaced."""
    lines = [f.header(len(rows), "confirmed facts" if confirmed else "facts")]
    for x in rows:
        end = until(x)
        lines.append(f"- [{x['subject']}] {x['fact']} (learned "
                     f"{f.when(x['conversation_at'], '%Y-%m-%d')}"
                     + (f", until {end}" if end else "") + ") " + f.said_at(x)
                     + written_label(written.get(x["conversation_id"]))
                     + meeting_label(meetings.get(x["conversation_id"])))
        for s in sources[x["id"]]:
            lines.append(f"  said again {f.when(s['conversation_at'], '%Y-%m-%d')} "
                         + f.said_at(s) + written_label(written.get(s["conversation_id"])))
        for old in history[x["id"]]:
            lines.append(f"  replaces: {old['fact']} (from "
                         f"{f.when(old['conversation_at'], '%Y-%m-%d')}) "
                         + f.said_at(old) + written_label(written.get(old["conversation_id"])))
    return cited(lines)


def digest(f: Fmt, date: str, d: dict[str, Any],
           convs: list[tuple[str, dict[str, Any] | None]]) -> str:
    """get_daily_digest; ``convs`` are (id, conversation or None) in the digest's order."""
    out = [f"{date}: {d['headline']}", d["summary"]]
    out += [f"* {h}" for h in d.get("highlights", [])]
    out += [f"TODO: {t}" for t in d.get("open_action_items", [])]
    for cid, conv in convs:
        what = ("Written note" if conv and conv.get("written") else
                "Voice note" if conv and conv.get("kind") == "note" else
                "Online meeting" if conv and conv.get("meeting") else "Conversation")
        out.append(f"{what}: {f.link(cid)}")
    out.append(f"{CITE}.")
    return "\n".join(out)


def people(f: Fmt, speakers: list[dict[str, Any]], enrolled: set[str],
           mentioned: list[dict[str, Any]]) -> str:
    """list_people, text format."""
    out = ["Speakers:"]
    out += [f"- {s['name']}{' (owner)' if s['is_owner'] else ''}"
            f"{' (voice known)' if s['name'] in enrolled else ''}: {s['lines']} lines in "
            f"{s['conversations']} conversations, last {f.when(s['last_heard'])}"
            for s in speakers]
    out.append("Mentioned:")
    out += [f"- {p['name']}: {p['conversations']} conversations, last "
            f"{f.when(p['last_mentioned'], '%Y-%m-%d')}" for p in mentioned]
    return "\n".join(out)


def renamed(old_name: str, new_name: str, n: int, convs: int, old_id: int | None,
            new_id: int | None, pid: int | None) -> str:
    """rename_speaker_everywhere, after a rename of ``n`` lines; ``old_id``/``new_id``
    the two names' people before it, ``pid`` the new name's person after it."""
    msg = (f"Renamed {old_name} to {new_name} in {n} lines across {convs} "
           "conversations; their summaries will be redone.")
    if pid is None:
        return msg
    if old_id is not None and new_id is not None and old_id != new_id:
        return msg + (f" {old_name} (person id {old_id}) is merged into {new_name}, "
                      f"whose person id {pid} is kept.")
    return msg + (f" The person id {pid} is kept." if old_id == pid else f" Person id {pid}.")


def unnamed_speaker(f: Fmt, k: dict[str, Any], conv: dict[str, Any], quotes: list[Any],
                    guess: tuple[str, float] | None) -> list[str]:
    """list_unnamed_speakers: one voice, its conversation and a few of its lines."""
    out = [f"- {k['label']} in \"{conv['title'] or 'untitled'}\" "
           f"{f.when(conv['started_at'])} (conversation id {conv['id']}), {k['lines']} "
           "lines" + (f"; sounds like {guess[0]} ({guess[1]:.2f})" if guess else "")]
    out += [f"  #{q.id} \"{q.text}\" " + f.link(conv["id"], q.started_at - conv["started_at"])
            for q in sorted(quotes, key=lambda s: s.started_at)]
    return out


# ---------------------------------------------------------------- pieces
def summary(c: dict[str, Any]) -> str:
    if c.get("forget_pending"):
        return "(being rewritten after the user forgot something; ask again later)"
    if c.get("transcript_pruned_at") and not c.get("summary"):
        return "(none; the transcript was removed by the retention setting)"
    return c["summary"] or "(pending)"


def title(c: dict[str, Any]) -> str:
    if c.get("forget_pending"):
        return "untitled (being rewritten after a forget)"
    if c.get("status") == "discarded":
        return f"(not summarized: looked like {c.get('discard_reason') or 'junk'})"
    return c.get("title") or "untitled (summary pending)"


def note(kind: str | None, written: dict[str, Any] | None = None) -> str:
    """Marks a voice note (the owner talking to themselves) next to its title, or a note
    written by hand in the hub, with the link to it there."""
    if written:
        return written_label(written)
    return " (voice note)" if kind == "note" else ""


def participants(meeting: dict[str, Any] | None) -> str:
    """"; participants: A, B" after a meeting's people in search_conversations."""
    if not meeting or not meeting.get("participants"):
        return ""
    return "; participants: " + ", ".join(p["name"] for p in meeting["participants"])


def attendance(meeting: dict[str, Any], when: Any) -> str:
    """get_conversation's participants: "Dana Example (16:00–16:45), Me", with the times
    they joined and left when the bot sent them."""
    out = []
    for p in meeting.get("participants") or []:
        span = "–".join(when(t, "%H:%M") for t in (p.get("joined_at"), p.get("left_at"))
                        if t is not None)
        out.append(p["name"] + (f" ({span})" if span else ""))
    return ", ".join(out) or "—"


def due(a: dict[str, Any]) -> str:
    """" due 2026-10-21 12:00 (Wednesday at 12 pm)", plus " [time unclear]" when set."""
    when_due = " ".join(x for x in (a.get("due_date"), a.get("due_time")) if x)
    if when_due:
        out = f" due {when_due}" + (f" ({a['due']})" if a.get("due") else "")
    else:
        out = f" due {a['due']}" if a.get("due") else ""
    return out + (" [time unclear]" if a.get("time_unclear") else "")


def kind_word(a: dict[str, Any]) -> str:
    """"event: " / "reminder: " before the text; nothing for a task."""
    kind = a.get("kind") or "task"
    return "" if kind == "task" else f"{kind}: "


def _src(f: Fmt, item: dict[str, Any], conv: dict[str, Any]) -> str:
    off = (item.get("source_at") or conv["started_at"]) - conv["started_at"]
    return f.link(conv["id"], off)


def _item(f: Fmt, a: dict[str, Any], conv: dict[str, Any]) -> str:
    kind = kind_word(a)
    return (f"Action #{a['id']} [{a['status']}]{' ' + kind if kind else ': '}{a['text']}"
            + (f" (owner {a['owner']})" if a.get("owner") else "")
            + due(a) + " " + _src(f, a, conv))
