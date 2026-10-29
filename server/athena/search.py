"""Embeddings (optional) and hybrid search over the store."""

from __future__ import annotations

from typing import Any, Protocol

import httpx
import numpy as np

from .db.store import Store


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> np.ndarray: ...


class OpenAIEmbedder:
    """llama-server started with ``--embeddings`` (or any local /v1/embeddings server)."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                  transport=transport)
        self.model = model

    def embed(self, texts: list[str]) -> np.ndarray:
        resp = self._http.post("/embeddings", json={"model": self.model, "input": texts})
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return np.asarray([d["embedding"] for d in data], dtype=np.float32)


def index_conversation(store: Store, embedder: Embedder, conv_id: str) -> int:
    """Embed a summarized conversation, its facts and action items. Returns item count."""
    conv = store.get_conversation(conv_id)
    if conv is None or conv["status"] != "summarized":
        return 0
    items: list[tuple[str, str, str]] = [
        ("conversation", conv_id,
         f"{conv['title']}\n{conv['summary']}\n" + "\n".join(conv["key_points"]))]
    items += [("fact", str(f["id"]), f"{f['subject'] or ''}: {f['fact']}") for f in conv["facts"]]
    items += [("action_item", str(a["id"]), a["text"]) for a in conv["action_items"]]
    vectors = embedder.embed([t for _, _, t in items])
    for (kind, ref, _), vec in zip(items, vectors, strict=True):
        store.put_embedding(kind, ref, conv_id, embedder.model, vec)
    return len(items)


def search(store: Store, query: str, embedder: Embedder | None = None,
           limit: int = 10) -> list[dict[str, Any]]:
    """Hybrid search: FTS5 keywords + (optional) cosine similarity, fused by RRF.

    Results are grouped per conversation so callers can link to the transcript.
    """
    rankings: list[list[tuple[str, str, str]]] = []
    snippets: dict[tuple[str, str], str] = {}
    fts = store.fts_search(query, limit=limit * 5)
    rankings.append([(r["kind"], r["ref_id"], r["conversation_id"]) for r in fts])
    for r in fts:
        snippets[(r["kind"], r["ref_id"])] = r["snippet"]
    if embedder is not None:
        rows = store.all_embeddings(embedder.model)
        if rows:
            q = embedder.embed([query])[0]
            q = q / (np.linalg.norm(q) or 1.0)
            mat = np.stack([v for *_, v in rows])
            mat = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)
            order = np.argsort(-(mat @ q))[: limit * 5]
            rankings.append([rows[i][:3] for i in order])
    fused: dict[tuple[str, str, str], float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
    by_conv: dict[str, dict[str, Any]] = {}
    for (kind, ref, conv_id), score in sorted(fused.items(), key=lambda kv: -kv[1]):
        hit = by_conv.setdefault(conv_id, {"conversation_id": conv_id, "score": 0.0,
                                           "matches": []})
        hit["score"] += score
        hit["matches"].append({"kind": kind, "ref_id": ref,
                               "snippet": snippets.get((kind, ref))})
    results = sorted(by_conv.values(), key=lambda h: -h["score"])[:limit]
    for hit in results:
        conv = store.get_conversation(hit["conversation_id"])
        if conv:
            hit.update(title=conv["title"], started_at=conv["started_at"],
                       ended_at=conv["ended_at"], people=conv["people"])
    return [h for h in results if "title" in h]
