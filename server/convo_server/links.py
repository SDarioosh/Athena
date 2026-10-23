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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings


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
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo(settings.timezone)).strftime(fmt)


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
section{{background:#fff;border:1px solid #e3e3e3;border-radius:.5rem;padding:.6rem .9rem;
margin:.8rem 0}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#ddd}}
section{{background:#202020;border-color:#333}} .line.hit{{background:#4a4000}}}}
</style></head><body>
<h1>{title}</h1><div class="meta">{when} · {people}</div>
<section>{summary}{lists}</section>
<section id="transcript">{lines}</section>
<script>
(function(){{var m=location.hash.match(/t=(\\d+)/);if(!m)return;var t=+m[1],best=null;
document.querySelectorAll('.line').forEach(function(e){{if(+e.dataset.t<=t)best=e;}});
if(best){{best.classList.add('hit');best.scrollIntoView({{block:'center'}});}}}})();
</script></body></html>"""


def render_conversation(settings: Settings, conv: dict[str, Any], segments: list[Any]) -> str:
    e = html.escape
    origin = conv["started_at"]

    def items(title: str, rows: list[str]) -> str:
        if not rows:
            return ""
        return f"<h3>{e(title)}</h3><ul>" + "".join(f"<li>{r}</li>" for r in rows) + "</ul>"

    lists = items("Key points", [e(k) for k in conv.get("key_points", [])])
    lists += items("Action items", [
        e(a["text"]) + (f" <em>({e(a['owner'])})</em>" if a.get("owner") else "")
        + (" ✔" if a.get("status") == "done" else "")
        for a in conv.get("action_items", [])])
    lists += items("Facts", [e(f["fact"]) for f in conv.get("facts", [])])
    lines = []
    for s in segments:
        off = int(s.started_at - origin)
        cls = "spk owner" if s.is_owner else "spk"
        lines.append(
            f'<div class="line" data-t="{off}"><span class="t">'
            f'{e(local_time(settings, s.started_at, "%H:%M:%S") or "")}</span>'
            f'<span class="{cls}">{e(s.speaker)}:</span>{e(s.text)}</div>')
    return _PAGE.format(
        title=e(conv.get("title") or "Untitled conversation"),
        when=e(f"{local_time(settings, origin)} – {local_time(settings, conv['ended_at'], '%H:%M')}"
               f" ({settings.timezone})"),
        people=e(", ".join(conv.get("people", [])) or "—"),
        summary=f"<p>{e(conv.get('summary') or _no_summary(conv))}</p>",
        lists=lists,
        lines="".join(lines) or (
            f"<p>The transcript was removed on {e(local_time(settings, pruned, '%Y-%m-%d') or '')}"
            " by the retention setting; the summary, facts and action items are kept.</p>"
            if (pruned := conv.get("transcript_pruned_at")) else "<p>No transcript.</p>"),
    )


def _no_summary(conv: dict) -> str:
    if conv.get("forget_pending"):
        return "Being rewritten after something here was forgotten."
    if conv.get("status") == "discarded":
        return (f"Not summarized: this looked like {conv.get('discard_reason') or 'junk'} "
                "(TV, noise or no real conversation).")
    return "Summary pending."
