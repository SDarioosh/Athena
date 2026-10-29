"""Full-text search and embeddings."""

from __future__ import annotations

from typing import Any

import numpy as np

from .matching import _STOPWORDS


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms.

    Stopwords are dropped unless nothing else is left ("the one about AI glasses" ->
    "AI" OR "glasses").
    """
    terms = [t for t in ("".join(ch for ch in w if ch.isalnum()) for w in text.split()) if t]
    topic = [t for t in terms if t.lower() not in _STOPWORDS]
    return " OR ".join(f'"{t}"' for t in topic or terms)


class SearchRepo:
    """``Store``'s methods for search and embeddings. A mixin: ``Store`` opens the connection
    they use (``tx``, ``_q``, ``_one``, ``_conn``, ``_lock``)."""

    def search_segments(self, *, query: str | None = None, speaker: str | None = None,
                        since: float | None = None, until: float | None = None,
                        limit: int = 20) -> list[dict[str, Any]]:
        """Transcript lines filtered by speaker/time, ranked by ``query`` if given.

        Without a query, lines come back in chronological order.
        """
        cols = ("s.id, s.conversation_id, s.started_at, s.ended_at, s.speaker, s.is_owner,"
                " s.text, s.kind, c.title AS conversation_title,"
                " c.started_at AS conversation_at, c.kind AS conversation_kind")
        where = ["s.conversation_id IS NOT NULL"]
        params: list[Any] = []
        if speaker:
            where.append("s.speaker = ? COLLATE NOCASE")
            params.append(speaker)
        if since is not None:
            where.append("s.started_at >= ?")
            params.append(since)
        if until is not None:
            where.append("s.started_at < ?")
            params.append(until)
        q = _fts_query(query or "")
        if q:
            sql = (f"SELECT {cols}, bm25(segments_fts) AS score FROM segments_fts"
                   " JOIN segments s ON s.id = segments_fts.rowid"
                   " JOIN conversations c ON c.id = s.conversation_id"
                   f" WHERE segments_fts MATCH ? AND {' AND '.join(where)}"
                   " ORDER BY score LIMIT ?")
            return self._q(sql, [q, *params, limit])
        sql = (f"SELECT {cols} FROM segments s JOIN conversations c ON c.id = s.conversation_id"
               f" WHERE {' AND '.join(where)} ORDER BY s.started_at LIMIT ?")
        return self._q(sql, [*params, limit])

    # ------------------------------------------------------------------ search
    def fts_search(self, query: str, limit: int = 20,
                   kind: str | None = None) -> list[dict[str, Any]]:
        """Best matches first; ``kind`` keeps one kind of entry (e.g. "fact"), so the
        limit isn't used up by transcripts and summaries that match the same words."""
        q = _fts_query(query)
        if not q:
            return []
        return self._q(
            "SELECT kind, ref_id, conversation_id, snippet(search_fts, 3, '[', ']', '…', 12)"
            " AS snippet, bm25(search_fts) AS score FROM search_fts WHERE search_fts MATCH ?"
            + (" AND kind = ?" if kind else "") + " ORDER BY score LIMIT ?",
            (q, *([kind] if kind else []), limit))

    def put_embedding(self, kind: str, ref_id: str, conversation_id: str, model: str,
                      vector: np.ndarray) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?)",
                      (kind, ref_id, conversation_id, model,
                       np.asarray(vector, dtype=np.float32).tobytes()))

    def all_embeddings(self, model: str) -> list[tuple[str, str, str, np.ndarray]]:
        rows = self._q("SELECT kind, ref_id, conversation_id, vector FROM embeddings"
                       " WHERE model = ?", (model,))
        return [(r["kind"], r["ref_id"], r["conversation_id"],
                 np.frombuffer(r["vector"], dtype=np.float32)) for r in rows]
