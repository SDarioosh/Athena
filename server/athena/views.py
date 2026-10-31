"""The HTML transcript page a signed link opens (``GET /view/<id>``), also the shared
copy ``export.py`` makes. Links and their signatures are in ``links.py``."""

from __future__ import annotations

import html
import time
from collections.abc import Collection
from typing import Any

from .config import Settings
from .links import local_time, meeting_label

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
    # a text thread can span days: each message shows its weekday too
    clock = "%a %H:%M" if conv.get("thread") else "%H:%M:%S"
    for s in segments:
        off = int(s.started_at - origin)
        cls = "spk owner" if s.is_owner else "spk"
        chat = getattr(s, "kind", "speech") == "chat"  # typed in the meeting chat
        lines.append(
            f'<div class="line{" chat" if chat else ""}" data-t="{off}"><span class="t">'
            f'{e(local_time(settings, s.started_at, clock) or "")}</span>'
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
                 + _meeting(conv.get("meeting")) + _thread(conv.get("thread"))),
        when=e(when_of(settings, conv)),
        people=e(", ".join(conv.get("people", [])) or "—"),
        sections=sections,
        script="" if shared else _JUMP_SCRIPT,
    )


def title_of(conv: dict[str, Any]) -> str:
    """A page's title; hidden while erase_fact's rewrite is pending, like the summary."""
    if conv.get("forget_pending"):
        return "Untitled conversation"
    if conv.get("thread") and (conv.get("status") == "discarded" or not conv.get("title")):
        from .texts.repo import plain_title

        return plain_title(conv["thread"])
    return conv.get("title") or "Untitled conversation"


def when_of(settings: Settings, conv: dict[str, Any]) -> str:
    """"2025-10-09 10:53 – 11:20 (Europe/Lisbon)"; the end's date too when it is
    another day (a text thread)."""
    start = local_time(settings, conv["started_at"]) or ""
    end = local_time(settings, conv["ended_at"]) or ""
    end = end[11:] if end[:10] == start[:10] else end
    return f"{start} – {end} ({settings.timezone})"


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


def _thread(thread: dict[str, Any] | None) -> str:
    """A thread of text messages: whose chat it is."""
    if not thread:
        return ""
    from .texts.repo import label

    return f'<div class="meta">{html.escape(label(thread))}</div>'


def _meeting(meeting: dict[str, Any] | None) -> str:
    """An online meeting: its topic and participants."""
    if not meeting:
        return ""
    e = html.escape
    people = ", ".join(p["name"] for p in meeting.get("participants") or [])
    return (f'<div class="meta">{e(meeting_label(meeting).strip(" ()"))}'
            + (f" · Participants: {e(people)}" if people else "") + "</div>")


def _no_summary(conv: dict) -> str:
    if conv.get("forget_pending"):
        return "Being rewritten after something here was forgotten."
    if conv.get("thread") and conv.get("status") == "discarded":
        return ("A short exchange; no summary." if conv.get("discard_reason") == "short" else
                "Not summarized: older than the summary window. The messages are below.")
    if conv.get("status") == "discarded":
        return (f"Not summarized: this looked like {conv.get('discard_reason') or 'junk'} "
                "(TV, noise or no real conversation).")
    return "Summary pending."
