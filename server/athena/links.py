"""Signed transcript links and the HTML transcript view they open.

Links look like ``<base>/view/<conversation_id>?sig=<hmac>#t=<seconds>`` so they can be
opened from a phone browser without the API bearer token. The signature is an HMAC of
the conversation ID keyed from ``CONVO_INGEST_TOKEN``; rotating the token revokes all
links. Serve the API on loopback/Tailscale only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from collections.abc import Collection
from typing import Any

from .config import Settings
from .timeutil import local_time as _local_time


def _key(settings: Settings) -> bytes:
    return hmac.new(settings.ingest_token.encode(), b"convo-transcript-links",
                    hashlib.sha256).digest()


def sign(settings: Settings, conv_id: str) -> str:
    return hmac.new(_key(settings), conv_id.encode(), hashlib.sha256).hexdigest()[:32]


def verify(settings: Settings, conv_id: str, sig: str) -> bool:
    return hmac.compare_digest(sign(settings, conv_id), sig or "")


def transcript_link(settings: Settings, conv_id: str, offset: float | None = None) -> str:
    url = f"{settings.base_url}/view/{conv_id}?sig={sign(settings, conv_id)}"
    return url + (f"#t={max(0, int(offset))}" if offset is not None else "")


def local_time(settings: Settings, ts: float | None, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    return _local_time(ts, settings.timezone, fmt)


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>
body{{font:16px/1.45 system-ui,sans-serif;max-width:46rem;margin:0 auto;padding:1rem;
color:#222;background:#fafafa}}
h1{{font-size:1.3rem;margin:.2rem 0}} .meta{{color:#666;font-size:.9rem}}
.line{{padding:.25rem .4rem;border-radius:.3rem;margin:.1rem 0}}
.line.hit{{background:#fff3b0}} .t{{color:#888;font-size:.8rem;margin-right:.4rem}}
.spk{{font-weight:600;margin-right:.3rem}} .owner{{color:#0a5}}
.chat .via{{color:#888;font-weight:400;font-size:.85rem}}
section{{background:#fff;border:1px solid #e3e3e3;border-radius:.5rem;padding:.6rem .9rem;
margin:.8rem 0}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#ddd}}
section{{background:#202020;border-color:#333}} .line.hit{{background:#4a4000}}}}
</style></head><body>
<h1>{title}</h1><div class="meta">{when} · {people}</div>{written}
{sections}{script}</body></html>"""

# Jumps to the line a link's #t=<seconds> points at. Left out of shared copies.
_JUMP_SCRIPT = """<script>
(function(){var m=location.hash.match(/t=(\\d+)/);if(!m)return;var t=+m[1],best=null;
document.querySelectorAll('.line').forEach(function(e){if(+e.dataset.t<=t)best=e;});
if(best){best.classList.add('hit');best.scrollIntoView({block:'center'});}})();
</script>"""

# What a page can hold; ``render_conversation(include=...)`` picks from these.
SECTIONS = ("summary", "action_items", "facts", "transcript")


def render_conversation(settings: Settings, conv: dict[str, Any], segments: list[Any], *,
                        include: Collection[str] = SECTIONS, shared: bool = False) -> str:
    """The transcript page. ``include`` picks sections from :data:`SECTIONS` (summary
    with key points, action items, facts, transcript). ``shared`` is a copy to send to
    other people (``export.py``): no jump script, no link back into the hub, and a line
    saying where it came from."""
    e = html.escape
    origin = conv["started_at"]

    def items(title: str, rows: list[str]) -> str:
        if not rows:
            return ""
        return f"<h3>{e(title)}</h3><ul>" + "".join(f"<li>{r}</li>" for r in rows) + "</ul>"

    hidden = bool(conv.get("forget_pending"))  # erase_fact: wait for the rewrite
    lists = items("Key points", [] if hidden else [e(k) for k in conv.get("key_points", [])])
    lists = lists if "summary" in include else ""
    lists += "" if "action_items" not in include else items("Action items", [
        e(a["text"]) + (f" <em>({e(a['owner'])})</em>" if a.get("owner") else "")
        + (" ✔" if a.get("status") == "done" else "")
        for a in conv.get("action_items", [])])
    if "facts" in include:
        lists += items("Facts", [e(f["fact"]) for f in conv.get("facts", [])])
    lines = []
    for s in segments:
        off = int(s.started_at - origin)
        cls = "spk owner" if s.is_owner else "spk"
        chat = getattr(s, "kind", "speech") == "chat"  # typed in the meeting chat
        lines.append(
            f'<div class="line{" chat" if chat else ""}" data-t="{off}"><span class="t">'
            f'{e(local_time(settings, s.started_at, "%H:%M:%S") or "")}</span>'
            f'<span class="{cls}">{e(s.speaker)}'
            f'{" <span class=via>(chat)</span>" if chat else ""}:</span>{e(s.text)}</div>')
    summary = (f"<p>{e(_no_summary(conv) if hidden else conv.get('summary') or _no_summary(conv))}"
               "</p>" if "summary" in include else "")
    sections = f"<section>{summary}{lists}</section>" if summary or lists else ""
    if "transcript" in include:
        sections += '\n<section id="transcript">' + ("".join(lines) or (
            f"<p>The transcript was removed on {e(local_time(settings, pruned, '%Y-%m-%d') or '')}"
            " by the retention setting; the summary, facts and action items are kept.</p>"
            if (pruned := conv.get("transcript_pruned_at")) else "<p>No transcript.</p>")
        ) + "</section>"
    if shared:
        sections += (f'\n<p class="meta">Shared from Athena on '
                     f"{e(local_time(settings, time.time(), '%Y-%m-%d') or '')}.</p>")
    return _PAGE.format(
        title=e(title_of(conv)),
        written=(_written(conv.get("written"), link=not shared)
                 + _meeting(conv.get("meeting"))),
        when=e(when_of(settings, conv)),
        people=e(", ".join(conv.get("people", [])) or "—"),
        sections=sections,
        script="" if shared else _JUMP_SCRIPT,
    )


def title_of(conv: dict[str, Any]) -> str:
    """A page's title; hidden while erase_fact's rewrite is pending, like the summary."""
    if conv.get("forget_pending"):
        return "Untitled conversation"
    return conv.get("title") or "Untitled conversation"


def when_of(settings: Settings, conv: dict[str, Any]) -> str:
    """"2025-10-09 10:53 – 11:20 (Europe/Lisbon)"."""
    return (f"{local_time(settings, conv['started_at'])} – "
            f"{local_time(settings, conv['ended_at'], '%H:%M')} ({settings.timezone})")


def _written(written: dict[str, Any] | None, *, link: bool = True) -> str:
    """A written note: says so, and links back to the note (and its handwriting) in the
    hub (not in a shared copy: ``link=False``). The link was checked to be http(s) when
    the note came in."""
    if not written:
        return ""
    e = html.escape
    if not link:
        return '<div class="meta">Written by hand</div>'
    label = f"“{written['title']}” in Odysseus" if written.get("title") else "the note in Odysseus"
    return (f'<div class="meta">Written by hand · <a href="{e(written["link"], quote=True)}"'
            f' rel="noreferrer noopener">Open {e(label)}</a></div>')


def _meeting(meeting: dict[str, Any] | None) -> str:
    """An online meeting: its topic and participants."""
    if not meeting:
        return ""
    e = html.escape
    people = ", ".join(p["name"] for p in meeting.get("participants") or [])
    return (f'<div class="meta">{e(meeting_label(meeting).strip(" ()"))}'
            + (f" · Participants: {e(people)}" if people else "") + "</div>")


def meeting_label(meeting: dict[str, Any] | None) -> str:
    """" (Online meeting: <topic>)" after a title, " (Online meeting)" without a topic; ""
    for anything else (MCP 0.17.0; 0.16.0 said "Zoom meeting"). It names no platform:
    meetings may come from others than Zoom later."""
    if not meeting:
        return ""
    return f" (Online meeting: {meeting['topic']})" if meeting.get("topic") else " (Online meeting)"


def speaker_label(segment: Any) -> str:
    """A line's speaker as shown: "Dana (chat)" for a meeting chat message."""
    kind = segment.get("kind") if isinstance(segment, dict) else getattr(segment, "kind", None)
    name = segment["speaker"] if isinstance(segment, dict) else segment.speaker
    return f"{name} (chat)" if kind == "chat" else name


def written_label(written: dict[str, Any] | None) -> str:
    """" (written by hand: <hub link>)" after a title or fact; "" for spoken ones."""
    return f" (written by hand: {written['link']})" if written else ""


def _no_summary(conv: dict) -> str:
    if conv.get("forget_pending"):
        return "Being rewritten after something here was forgotten."
    if conv.get("status") == "discarded":
        return (f"Not summarized: this looked like {conv.get('discard_reason') or 'junk'} "
                "(TV, noise or no real conversation).")
    return "Summary pending."
