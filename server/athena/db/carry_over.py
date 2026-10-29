"""What a re-summary keeps of a conversation's old action items and facts."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .matching import _norm, _same


def _same_item(before: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    """What a re-summarized action item keeps from the matching old one (and takes it
    out of ``before``): its id (the hub files items by id), status, when it was first
    stored, whether it was explicit, and the kind, date, time, list and title if the user
    changed them."""
    # the same text first (a similar item said nearby must not take its row); then
    # closed or edited ones: they are what the user would lose
    text = _norm(item["text"])
    old = next((o for o in sorted(before, key=lambda o: (_norm(o["text"]) != text,
                                                         o["status"] == "open",
                                                         not o["edited"], o["id"]))
                if _same(item["text"], item.get("source_at"), o["text"], o["source_at"])),
               None)
    if old is None:
        return {}
    before.remove(old)
    keep = {"id": old["id"], "status": old["status"],
            "created_at": old["created_at"] or time.time()}
    if old["explicit"]:  # the hub filed it as the user's: it stays that
        keep["explicit"] = 1
    if old["edited"]:
        keep.update({k: old[k] for k in ("kind", "due_date", "due_time", "time_unclear",
                                         "list_name", "title", "note_title", "edited")})
    return keep


def _insert_fact(c: sqlite3.Connection, conv_id: str, subject: str | None, fact: str,
                 source_at: float | None, fact_id: int | None = None,
                 kind: str | None = None) -> int:
    cur = c.execute("INSERT INTO facts (id, conversation_id, subject, fact, source_at, kind)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (fact_id, conv_id, subject, fact, source_at, kind))
    c.execute("INSERT INTO search_fts VALUES ('fact', ?, ?, ?)",
              (str(cur.lastrowid), conv_id, f"{subject or ''}: {fact}"))
    return int(cur.lastrowid or 0)


def _retire(c: sqlite3.Connection, fact_id: int, at: float | None,
            reason: str | None) -> None:
    """Mark a fact retired and take it out of search (index and embeddings)."""
    c.execute("UPDATE facts SET status = 'retired', superseded_by = NULL, retired_at = ?,"
              " retired_reason = ? WHERE id = ?", (at, reason, fact_id))
    c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?", (str(fact_id),))
    c.execute("DELETE FROM embeddings WHERE kind = 'fact' AND ref_id = ?", (str(fact_id),))


def _chain_end(c: sqlite3.Connection, fact_id: int) -> int:
    """The fact at the end of ``fact_id``'s ``superseded_by`` chain (itself if it isn't
    replaced). Stops at a loop or a missing row."""
    seen = {fact_id}
    while True:
        row = c.execute("SELECT status, superseded_by FROM facts WHERE id = ?",
                        (fact_id,)).fetchone()
        nxt = row["superseded_by"] if row and row["status"] == "superseded" else None
        if nxt is None or nxt in seen:
            return fact_id
        seen.add(nxt)
        fact_id = nxt


def _knowledge(c: sqlite3.Connection, conv_id: str) -> dict[str, Any]:
    """What a re-summary must not lose: closed action items and the facts' links."""
    facts = [dict(r) for r in c.execute(
        "SELECT id, subject, fact, source_at, status, superseded_by, retired_at,"
        " retired_reason, erased_at, kind FROM facts WHERE conversation_id = ? ORDER BY id",
        (conv_id,))]
    for f in facts:
        f["sources"] = [dict(r) for r in c.execute(
            "SELECT s.conversation_id, s.source_at FROM fact_sources s JOIN conversations v"
            " ON v.id = s.conversation_id WHERE s.fact_id = ? ORDER BY v.started_at",
            (f["id"],))]
        f["replaced"] = [r["id"] for r in c.execute(
            "SELECT id FROM facts WHERE superseded_by = ?", (f["id"],))]
    return {
        "items": [dict(r) for r in c.execute(
            "SELECT id, text, source_at, status, created_at, kind, due_date, due_time,"
            " time_unclear, list_name, title, note_title, explicit, edited FROM action_items"
            " WHERE conversation_id = ?"
            " ORDER BY id",
            (conv_id,))],
        "facts": facts,
        # facts of other conversations that this one said again ("same" links)
        "said_again": [dict(r) for r in c.execute(
            "SELECT s.fact_id, s.source_at, f.fact FROM fact_sources s JOIN facts f"
            " ON f.id = s.fact_id WHERE s.conversation_id = ?", (conv_id,))],
    }


def _carry_over(c: sqlite3.Connection, conv_id: str, before: dict[str, Any],
                fact_ids: list[int]) -> list[int]:
    """Re-apply ``before`` (from ``_knowledge``) to the new items; returns the new fact
    ids that are still unlinked."""
    new = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, subject, fact, source_at FROM facts WHERE id IN"
        f" ({','.join('?' * len(fact_ids))})", fact_ids)} if fact_ids else {}
    fresh = [i for i in fact_ids if i in new]

    def match(fact: str, source_at: float | None) -> int | None:
        exact = next((i for i in fresh if _norm(new[i]["fact"]) == _norm(fact)), None)
        return exact or next((i for i in fresh if _same(new[i]["fact"], new[i]["source_at"],
                                                         fact, source_at)), None)

    for old in before["facts"]:
        target = match(old["fact"], old["source_at"])
        sources = old["sources"]
        if target is not None:
            fresh.remove(target)
            # said again: the new row takes the old id, so the hub sees the same fact
            c.execute("UPDATE facts SET id = ? WHERE id = ?", (old["id"], target))
            c.execute("UPDATE search_fts SET ref_id = ? WHERE kind = 'fact' AND ref_id = ?",
                      (str(old["id"]), str(target)))
            target = old["id"]
        elif sources:  # said elsewhere too: keep it, owned by the first other source
            first, sources = sources[0], sources[1:]
            target = _insert_fact(c, first["conversation_id"], old["subject"], old["fact"],
                                  first["source_at"], fact_id=old["id"], kind=old["kind"])
        elif old["status"] == "retired":
            # not said this time: keep the retired row, so a later re-summary that says it
            # again still finds it retired
            target = _insert_fact(c, conv_id, old["subject"], old["fact"], old["source_at"],
                                  fact_id=old["id"], kind=old["kind"])
        else:
            continue
        if old["status"] == "retired":
            _retire(c, target, old["retired_at"], old["retired_reason"])
        if old["erased_at"] is not None:  # erased stays erased, under its new id
            c.execute("UPDATE facts SET erased_at = ? WHERE id = ?", (old["erased_at"], target))
            c.execute("UPDATE OR IGNORE erased_sources SET fact_id = ? WHERE fact_id = ?",
                      (target, old["id"]))
        if old["status"] == "superseded" and (old["superseded_by"] is None or c.execute(
                "SELECT 1 FROM facts WHERE id = ?", (old["superseded_by"],)).fetchone()):
            # (by nothing: a forgotten fact of a deleted note had replaced it)
            c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                      (old["superseded_by"], target))
        c.executemany("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                      [(target, s["conversation_id"], s["source_at"]) for s in sources])
        if old["replaced"]:
            c.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id IN"
                      f" ({','.join('?' * len(old['replaced']))})", (target, *old["replaced"]))
    for said in before["said_again"]:
        hit = match(said["fact"], said["source_at"])
        if hit is not None:  # the same statement again: stays a source, not a new fact
            fresh.remove(hit)
            c.execute("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                      (said["fact_id"], conv_id, new[hit]["source_at"]))
            c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?", (str(hit),))
            c.execute("DELETE FROM facts WHERE id = ?", (hit,))
    return fresh
