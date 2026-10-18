"""Speaker diarization with an enrolled owner voice.

Approach (kept deliberately simple and fully local):

1. Every transcript piece of at least ``min_seconds`` gets a speaker embedding
   (ECAPA-TDNN via SpeechBrain in production).
2. Pieces are grouped with average-linkage agglomerative clustering on cosine
   similarity.
3. Each cluster centroid is matched against enrolled voices (the owner and any
   named people). Unmatched clusters become "Speaker 1", "Speaker 2", ...
4. Pieces too short to embed inherit the label of the nearest piece in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .audio import SAMPLE_RATE


class SpeakerEmbedder(Protocol):
    def embed(self, audio: np.ndarray) -> np.ndarray: ...


class EcapaEmbedder:
    def __init__(self, cache_dir: Path) -> None:
        try:
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError as e:  # pragma: no cover - optional extra
            raise RuntimeError(
                "Diarization needs the 'diarization' extra: pip install -e '.[diarization]'"
            ) from e
        self._torch = torch
        self._model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(cache_dir / "ecapa"),
            run_opts={"device": "cpu"},
        )

    def embed(self, audio: np.ndarray) -> np.ndarray:  # pragma: no cover - needs model
        with self._torch.no_grad():
            wav = self._torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
            vec = self._model.encode_batch(wav).squeeze().cpu().numpy()
        return _normalize(vec)


@dataclass(frozen=True)
class EnrolledVoice:
    name: str
    embedding: np.ndarray
    is_owner: bool


@dataclass(frozen=True)
class SpeakerLabel:
    name: str
    is_owner: bool


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cluster(embeddings: np.ndarray, threshold: float) -> list[int]:
    """Average-linkage agglomerative clustering; returns a cluster id per row."""
    n = len(embeddings)
    if n == 0:
        return []
    x = np.stack([_normalize(e) for e in embeddings])
    sim = x @ x.T
    clusters: list[list[int]] = [[i] for i in range(n)]
    while len(clusters) > 1:
        best, pair = -2.0, (0, 0)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                s = float(sim[np.ix_(clusters[a], clusters[b])].mean())
                if s > best:
                    best, pair = s, (a, b)
        if best < threshold:
            break
        a, b = pair
        clusters[a].extend(clusters.pop(b))
    labels = [0] * n
    # number clusters by first appearance so labels are stable/readable
    for cid, members in enumerate(sorted(clusters, key=min)):
        for m in members:
            labels[m] = cid
    return labels


class Diarizer:
    def __init__(self, embedder: SpeakerEmbedder, voices: list[EnrolledVoice],
                 match_threshold: float = 0.50, cluster_threshold: float = 0.45,
                 min_seconds: float = 1.0) -> None:
        self.embedder = embedder
        self.voices = voices
        self.match_threshold = match_threshold
        self.cluster_threshold = cluster_threshold
        self.min_seconds = min_seconds

    def label(self, clips: list[np.ndarray], times: list[float]) -> list[SpeakerLabel]:
        """Label each clip; ``times`` (clip midpoints) are used for short-clip fallback."""
        if not clips:
            return []
        min_len = int(self.min_seconds * SAMPLE_RATE)
        idx = [i for i, c in enumerate(clips) if c.size >= min_len]
        if not idx:
            # nothing long enough on its own: treat all clips as one speaker
            joint = np.concatenate(clips)
            label = SpeakerLabel("Speaker 1", False)
            if joint.size >= min_len:
                label = self._match(self.embedder.embed(joint), set()) or label
            return [label] * len(clips)
        embs = np.stack([self.embedder.embed(clips[i]) for i in idx])
        cids = cluster(embs, self.cluster_threshold)
        names: dict[int, SpeakerLabel] = {}
        unknown = 0
        taken: set[str] = set()
        for cid in sorted(set(cids)):
            centroid = _normalize(embs[[j for j, c in enumerate(cids) if c == cid]].mean(axis=0))
            label = self._match(centroid, taken)
            if label is None:
                unknown += 1
                label = SpeakerLabel(f"Speaker {unknown}", False)
            else:
                taken.add(label.name)
            names[cid] = label
        out: list[SpeakerLabel | None] = [None] * len(clips)
        for j, i in enumerate(idx):
            out[i] = names[cids[j]]
        t = np.asarray(times)
        idx_arr = np.asarray(idx)
        for i in range(len(clips)):
            if out[i] is None:
                nearest = idx_arr[np.argmin(np.abs(t[idx_arr] - t[i]))]
                out[i] = out[int(nearest)]
        return [o for o in out if o is not None]

    def _match(self, centroid: np.ndarray, taken: set[str]) -> SpeakerLabel | None:
        best: tuple[float, EnrolledVoice] | None = None
        for v in self.voices:
            if v.name in taken:
                continue
            s = float(centroid @ _normalize(v.embedding))
            if s >= self.match_threshold and (best is None or s > best[0]):
                best = (s, v)
        return SpeakerLabel(best[1].name, best[1].is_owner) if best else None


def load_voices(store: Any) -> list[EnrolledVoice]:
    """Enrolled voices from ``Store.speakers()``."""
    return [EnrolledVoice(s["name"], s["embedding"], s["is_owner"]) for s in store.speakers()]


def enrollment_embedding(embedder: SpeakerEmbedder, audio: np.ndarray,
                         window_seconds: float = 3.0) -> np.ndarray:
    """Average embedding over windows of an enrollment recording (>= ~20 s of speech)."""
    win = int(window_seconds * SAMPLE_RATE)
    if audio.size < win:
        raise ValueError("enrollment audio must contain at least a few seconds of speech")
    vecs = [embedder.embed(audio[i:i + win]) for i in range(0, audio.size - win + 1, win)]
    return _normalize(np.mean(vecs, axis=0))
