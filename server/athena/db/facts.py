"""Facts: links between them, retiring, listing and counting."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .carry_over import _chain_end, _retire
from .matching import _STOPWORDS, _cited_line, _norm

# a fact's transcript line in list_facts JSON (``quote``) is cut to this many characters
QUOTE_CHARS = 300


class FactsRepo:
    """``Store``'s methods for facts. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    # ------------------------------------------------------------- fact links
    def facts_by_id(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = list(ids)
        if not ids:
            return []
        return self._q(
            "SELECT f.*, c.started_at AS conversation_at FROM facts f JOIN conversations c"
            f" ON c.id = f.conversation_id WHERE f.id IN ({','.join('?' * len(ids))})"
            " ORDER BY f.id", ids)

    def related_facts(self, conv_id: str, facts: list[dict[str, Any]],
                      limit: int = 15) -> list[dict[str, Any]]:
        """Current and retired facts of other conversations that may repeat or conflict
        with ``facts``: same subject, or matching words. Retired facts are offered too, so
        a forgotten fact said again is linked "same" to it and stays hidden."""
        ids: dict[int, None] = {}
        subjects = {(f.get("subject") or "").strip().lower() for f in facts} - {""}
        for subj in subjects:
            for r in self._q("SELECT id FROM facts WHERE status IN ('current', 'retired')"
                             " AND conversation_id != ? AND lower(subject) = ?"
                             " ORDER BY id DESC LIMIT ?", (conv_id, subj, limit)):
                ids[r["id"]] = None
        for f in facts:
            # this conversation's own facts match best: room for 5 others
            for hit in self.fts_search(f"{f.get('subject') or ''} {f['fact']}",
                                       limit=5 + len(facts), kind="fact"):
                if hit["conversation_id"] != conv_id:
                    ids[int(hit["ref_id"])] = None
        # retired facts are out of the search index: match their words here (few rows)
        words = {w for f in facts for w in _norm(f"{f.get('subject') or ''} {f['fact']}").split()
                 if w not in _STOPWORDS}
        for r in self._q("SELECT id, subject, fact FROM facts WHERE status = 'retired'"
                         " AND conversation_id != ?", (conv_id,)):
            if words & set(_norm(f"{r['subject'] or ''} {r['fact']}").split()):
                ids[r["id"]] = None
        rows = [r for r in self.facts_by_id(ids) if r["status"] in ("current", "retired")]
        return rows[:limit]

    def link_fact(self, new_id: int, relation: str, existing_id: int) -> None:
        """Record that fact ``new_id`` repeats ("same") or updates ("updates") a known one.

        "same": the new row is dropped and its conversation becomes another source of
        the known fact; a repeat of a retired fact so stays hidden. "updates": the older
        of the two (by conversation time) is marked superseded by the newer, so
        re-summarizing an old conversation can't overwrite newer knowledge. A newer fact
        that updates a retired one is a new belief and is shown; the retired one stays
        retired.
        """
        audio: list[str] = []
        with self.tx() as c:
            rows = {r["id"]: r for r in c.execute(
                "SELECT f.id, f.conversation_id, f.source_at, f.status, f.erased_at,"
                " f.kind, c.started_at FROM facts f"
                " JOIN conversations c ON c.id = f.conversation_id WHERE f.id IN (?, ?)",
                (new_id, existing_id))}
            if len(rows) != 2:
                return
            new, old = rows[new_id], rows[existing_id]
            if relation == "same":
                c.execute("INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?)",
                          (existing_id, new["conversation_id"], new["source_at"]))
                c.execute("DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = ?",
                          (str(new_id),))
                c.execute("DELETE FROM facts WHERE id = ?", (new_id,))
                if new["kind"] and new["kind"] != "other" and old["kind"] in (None, "other"):
                    # said again with a kind: an older fact without one takes it
                    c.execute("UPDATE facts SET kind = ? WHERE id = ?",
                              (new["kind"], existing_id))
                if old["erased_at"] is not None:  # said again after a forget: erase it too
                    self._erase_mentions(c, existing_id, time.time(), audio)
            elif relation == "updates":
                newer, older = (new, old) if new["started_at"] >= old["started_at"] else (old, new)
                if older["status"] == "retired":  # forgotten: not part of any history
                    return
                # the newer one may itself be replaced already (links of a conversation
                # processed late): point at the end of its chain, so every replaced fact
                # points at a current one
                target = _chain_end(c, newer["id"])
                if target == older["id"]:
                    return
                c.execute("UPDATE facts SET status = 'superseded', superseded_by = ?"
                          " WHERE id = ?", (target, older["id"]))
                # whatever the older one had replaced now points at the newer one
                c.execute("UPDATE facts SET superseded_by = ? WHERE superseded_by = ?",
                          (target, older["id"]))
        if relation == "same" and rows[existing_id]["erased_at"] is not None:
            self._after_erase(audio)

    def fact_history(self, fact_id: int) -> list[dict[str, Any]]:
        """Facts this one replaced (newest first), each with its conversation."""
        return self._q(
            "SELECT f.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM facts f JOIN conversations c ON c.id = f.conversation_id"
            " WHERE f.superseded_by = ? ORDER BY c.started_at DESC", (fact_id,))

    def fact_sources(self, fact_id: int) -> list[dict[str, Any]]:
        """Other conversations that said the same fact again, oldest first."""
        return self._q(
            "SELECT s.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM fact_sources s JOIN conversations c ON c.id = s.conversation_id"
            " WHERE s.fact_id = ? ORDER BY c.started_at", (fact_id,))

    def retire_fact(self, fact_id: int, reason: str = "") -> str | None:
        """Forget a fact (the owner said it's wrong or private): status 'retired', out of
        ``list_facts``, search and the conversation's facts. Returns "retired",
        "already" (it was retired before) or None (no such fact).

        It stays retired when its conversation is re-summarized, and a later "same" fact
        becomes another source of it instead of a new fact. Facts it had replaced stay
        replaced (they are older values of a belief the owner rejected).
        """
        with self.tx() as c:
            row = c.execute("SELECT status FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "retired":
                return "already"
            _retire(c, fact_id, time.time(), reason.strip() or None)
        return "retired"

    def list_facts(self, *, subject: str | None = None, query: str | None = None,
                   limit: int = 50, counts_tz: str | None = None,
                   min_sources: int = 0, min_days: int = 0) -> list[dict[str, Any]]:
        """Current facts matching ``subject``/``query``, newest first.

        A match on an older, replaced fact returns the fact that replaced it. With
        ``counts_tz`` each fact also gets its ``fact_counts`` (local dates in that
        timezone); ``min_sources``/``min_days`` then keep only facts said in at least that
        many conversations on at least that many dates ("confirmed" facts).
        """
        rows = self._list_current_facts(subject, query, -1 if counts_tz else limit)
        if counts_tz is None:
            return rows
        counts = self.fact_counts([r["id"] for r in rows], counts_tz)
        out = []
        for r in rows:
            r.update(counts[r["id"]])
            if r["times_said"] >= min_sources and r["days"] >= min_days:
                out.append(r)
        return out[:limit] if limit >= 0 else out

    def fact_counts(self, ids: Iterable[int], tz: str) -> dict[int, dict[str, Any]]:
        """For each fact: in how many distinct conversations it was said (its own plus
        its ``fact_sources``), on how many distinct local dates (``tz``), and the first
        and last of those dates (YYYY-MM-DD). One query for the whole set."""
        ids = list(ids)
        if not ids:
            return {}
        zone = ZoneInfo(tz)
        marks = ",".join("?" * len(ids))
        said: dict[int, dict[str, str]] = {i: {} for i in ids}
        for r in self._q(
                "SELECT f.id AS fact_id, c.id AS conv, c.started_at FROM facts f"
                f" JOIN conversations c ON c.id = f.conversation_id WHERE f.id IN ({marks})"
                " UNION SELECT s.fact_id, c.id, c.started_at FROM fact_sources s"
                f" JOIN conversations c ON c.id = s.conversation_id WHERE s.fact_id IN ({marks})",
                [*ids, *ids]):
            said[r["fact_id"]][r["conv"]] = (
                datetime.fromtimestamp(r["started_at"], zone).strftime("%Y-%m-%d"))
        out: dict[int, dict[str, Any]] = {}
        for fid, convs in said.items():
            days = sorted(set(convs.values()))
            out[fid] = {"times_said": len(convs), "days": len(days),
                        "first_said": days[0] if days else None,
                        "last_said": days[-1] if days else None}
        return out

    def fact_quotes(self, facts: Iterable[dict[str, Any]]) -> dict[int, str | None]:
        """For each fact (``id``, ``conversation_id``, ``source_at``, ``fact``): the
        transcript line it was said on (``_cited_line``, the line ``erase_fact`` would
        redact), so a reviewer can judge it without opening the transcript. When its own
        conversation has no transcript left (pruned), the newest conversation that said
        it again is used. None when there is no such line or it was erased. One query per
        conversation."""
        facts = list(facts)
        lines: dict[str, list[sqlite3.Row]] = {}

        def rows(conv_id: str) -> list[sqlite3.Row]:
            if conv_id not in lines:
                with self._lock:
                    lines[conv_id] = self._conn.execute(
                        "SELECT id, session_id, started_at, ended_at, erased, text"
                        " FROM segments WHERE conversation_id = ? ORDER BY started_at",
                        (conv_id,)).fetchall()
            return lines[conv_id]

        out: dict[int, str | None] = {}
        for f in facts:
            said = [(f["conversation_id"], f.get("source_at"))]
            if not rows(f["conversation_id"]):
                said += [(r["conversation_id"], r["source_at"]) for r in self._q(
                    "SELECT s.conversation_id, s.source_at FROM fact_sources s"
                    " JOIN conversations c ON c.id = s.conversation_id WHERE s.fact_id = ?"
                    " ORDER BY c.started_at DESC", (f["id"],))]
            line = None
            for conv_id, at in said:
                if at is None or not rows(conv_id):
                    continue
                hit = _cited_line(rows(conv_id), at, f.get("fact") or "")
                if hit is not None and not hit["erased"]:
                    line = " ".join(str(hit["text"] or "").split())[:QUOTE_CHARS] or None
                break
            out[f["id"]] = line
        return out

    def facts_like(self, conv_id: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stored facts a candidate fact of conversation ``conv_id`` may repeat or update
        (the summarizer's cap spares those): ``related_facts``, this conversation's own
        current facts (a re-summary) and the facts they had replaced."""
        own = self._q("SELECT id, subject, fact FROM facts WHERE conversation_id = ?"
                      " AND status = 'current'", (conv_id,))
        replaced = self._q(
            "SELECT id, subject, fact FROM facts WHERE superseded_by IN"
            " (SELECT id FROM facts WHERE conversation_id = ?)", (conv_id,))
        return self.related_facts(conv_id, facts) + own + replaced

    def _list_current_facts(self, subject: str | None, query: str | None,
                            limit: int) -> list[dict[str, Any]]:
        sql = "SELECT id, status, superseded_by FROM facts WHERE 1=1"
        params: list[Any] = []
        if subject:
            sql += " AND subject LIKE ? COLLATE NOCASE"
            params.append(f"%{subject}%")
        if query:
            ids = [r["ref_id"] for r in self.fts_search(query, limit=200, kind="fact")]
            if not ids:
                return []
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += [int(i) for i in ids]
        rows = self._q(sql, params)
        with self._lock:  # a match on a replaced fact gives the end of its chain
            current = {_chain_end(self._conn, r["id"]) if r["status"] == "superseded"
                       else r["id"] for r in rows}
        if not current:
            return []
        return self._q(
            "SELECT f.*, c.title AS conversation_title, c.started_at AS conversation_at"
            " FROM facts f JOIN conversations c ON c.id = f.conversation_id"
            f" WHERE f.status = 'current' AND f.id IN ({','.join('?' * len(current))})"
            " ORDER BY c.started_at DESC, f.id LIMIT ?", [*current, limit])
