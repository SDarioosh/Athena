"""Speaker diarization with enrolled voices.

Two backends behind one interface (``Diarizer.diarize_runs``):

* ``Diarizer`` (default, ``CONVO_DIARIZER=ecapa``): every transcript piece gets an
  ECAPA-TDNN embedding of a >= 3 s window around it; pieces are grouped with
  average-linkage agglomerative clustering on cosine similarity.
* ``PyannoteDiarizer`` (``CONVO_DIARIZER=pyannote``, optional extra, see
  ``diarize_pyannote.py``): pyannote.audio finds who spoke when on the whole recording;
  each pyannote speaker then gets an ECAPA embedding from its own speech.

Either way, each speaker cluster is then named the same way (``name_clusters``):

1. It is matched against enrolled voices. A person has several voice samples (enrollment
   windows, named speakers); the score is the mean of the closest few, so one odd sample
   doesn't decide. The best (cluster, person) pairs are assigned first.
2. Owner hint: the wearer is usually the loudest voice. If the owner wasn't matched and
   the clearly loudest cluster is a borderline match for the owner's voice, it is the
   owner.
3. Unmatched clusters become "Speaker 1", "Speaker 2", ... and their voice embedding is
   returned so the speaker can be named later and recognised from then on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

# Speaker embeddings on 1-2 s whisper segments are too noisy (one fast talker split into
# 7 "speakers" in testing); embedding a >= 3 s window around each segment fixes that.
DIARIZATION_WINDOW = 3.0
# A person's score is the mean similarity of their closest voice samples: up to this
# many, and at most a third of them (so 1 of 2 samples, 3 of 9 or more).
TOP_SAMPLES = 3
# A line the voice check unnamed (CONVO_SPEAKER_LINE_CHECK): a placeholder name, so it is
# never taken for a person.
UNSURE_LABEL = "Unknown"
# Line splitting at pyannote turn changes (CONVO_SPEAKER_SPLIT_LINES): a shorter part
# joins its neighbour.
SPLIT_MIN_SECONDS = 1.5

Word = tuple[float, float, str]  # start, end (seconds in the run), text


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
    # (n, dim) voice samples; empty = use ``embedding`` alone
    samples: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))

    def score(self, vec: np.ndarray) -> float:
        """Similarity of ``vec`` to this voice: mean of the closest few samples."""
        vec = _normalize(vec)
        if vec.size != np.asarray(self.embedding).size:
            return -1.0  # a voice of another speaker model never matches
        if self.samples.size == 0:
            return float(vec @ _normalize(self.embedding))
        sims = _normalize_rows(self.samples) @ vec
        k = max(1, min(TOP_SAMPLES, len(sims) // 3))
        return float(np.sort(sims)[-k:].mean())


@dataclass(frozen=True)
class SpeakerLabel:
    name: str
    is_owner: bool


@dataclass(frozen=True)
class PieceRef:
    """A transcript piece: which run (contiguous audio) and where in it, in seconds."""
    run: int
    start: float
    end: float


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n > 0, n, 1)


def rms_db(audio: np.ndarray) -> float:
    """Loudness of a clip in dBFS (silence = -120)."""
    if audio.size == 0:
        return -120.0
    return float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(audio)))), 1e-6)))


def window(audio: np.ndarray, start: float, end: float,
           min_seconds: float = DIARIZATION_WINDOW) -> np.ndarray:
    """Slice [start, end] seconds, widened symmetrically to ``min_seconds`` where possible."""
    sr = SAMPLE_RATE
    total = audio.size / sr
    half = max((end - start) / 2, min_seconds / 2)
    mid = (start + end) / 2
    a, b = max(0.0, mid - half), min(total, mid + half)
    if b - a < min_seconds:  # near an edge: extend towards the other side
        a, b = max(0.0, b - min_seconds), min(total, a + min_seconds)
    return audio[int(a * sr): int(b * sr)]


def cluster(embeddings: np.ndarray, threshold: float,
            cannot_link: list[int] | None = None) -> list[int]:
    """Average-linkage agglomerative clustering; returns a cluster id per row.

    Keeps the sum of pairwise similarities between clusters, so a merge updates one row
    and column instead of recomputing every pair (fast enough for thousands of pieces).

    ``cannot_link`` (one group id per row): two clusters that hold rows of the same group
    are never merged.
    """
    n = len(embeddings)
    if n == 0:
        return []
    x = np.stack([_normalize(e) for e in embeddings])
    sums = (x @ x.T).astype(np.float64)  # sums[a, b]: total similarity of a's and b's rows
    sizes = np.ones(n)
    active = np.ones(n, dtype=bool)
    members: list[list[int]] = [[i] for i in range(n)]
    upper = np.triu(np.ones((n, n), dtype=bool), k=1)
    groups = [{g} for g in cannot_link] if cannot_link is not None else None
    while active.sum() > 1:
        idx = np.flatnonzero(active)  # a cluster sits at its first member's row
        k = idx.size
        mean = sums[np.ix_(idx, idx)] / np.outer(sizes[idx], sizes[idx])
        mean[~upper[:k, :k]] = -np.inf
        if groups is not None:
            for i in range(k):
                for j in range(i + 1, k):
                    if groups[idx[i]] & groups[idx[j]]:
                        mean[i, j] = -np.inf
        i, j = divmod(int(np.argmax(mean)), k)  # first best pair, as the pairwise loop
        if mean[i, j] < threshold:
            break
        a, b = int(idx[i]), int(idx[j])
        sums[a, :] += sums[b, :]
        sums[:, a] += sums[:, b]
        sizes[a] += sizes[b]
        active[b] = False
        members[a].extend(members[b])
        if groups is not None:
            groups[a] |= groups[b]
    labels = [0] * n
    # number clusters by first appearance so labels are stable/readable
    for cid, a in enumerate(np.flatnonzero(active)):
        for m in members[a]:
            labels[m] = cid
    return labels


def absorb_singletons(embs: np.ndarray, cids: list[int], threshold: float) -> list[int]:
    """Move one-piece clusters into the closest cluster of several pieces when they are at
    least ``threshold`` similar to its centroid (0 = off).

    A single noisy embedding (a short or overlapped piece) often ends up alone and becomes
    an extra "Speaker N" with one line. Cluster ids are renumbered by first appearance.
    """
    if threshold <= 0 or len(cids) < 3:
        return cids
    members: dict[int, list[int]] = {}
    for j, c in enumerate(cids):
        members.setdefault(c, []).append(j)
    big = [c for c, m in members.items() if len(m) > 1]
    if not big:
        return cids
    cents = np.stack([_normalize(embs[members[c]].mean(axis=0)) for c in big])
    out = list(cids)
    for m in members.values():
        if len(m) == 1:
            sims = cents @ _normalize(embs[m[0]])
            if float(sims.max()) >= threshold:
                out[m[0]] = big[int(np.argmax(sims))]
    order: dict[int, int] = {}
    return [order.setdefault(c, len(order)) for c in out]


class Diarizer:
    # Opt-in naming rules (set from CONVO_* settings by ``cli.build_diarizer``):
    # a speaker is named only if its best voice beats the runner-up voice by this much
    # (CONVO_SPEAKER_MATCH_MARGIN, 0 = off) ...
    match_margin: float = 0.0
    # ... and only with this much speech (CONVO_SPEAKER_NAME_MIN_SECONDS, pyannote, 0 = off)
    name_min_seconds: float = 0.0
    # the shortest part a line split leaves (CONVO_SPEAKER_SPLIT_MIN_SECONDS)
    split_min_seconds: float = SPLIT_MIN_SECONDS

    def __init__(self, embedder: SpeakerEmbedder, voices: list[EnrolledVoice],
                 match_threshold: float = 0.50, cluster_threshold: float = 0.45,
                 min_seconds: float = 1.0, owner_hint_margin: float = 0.10,
                 owner_hint_db: float = 3.0, absorb_threshold: float = 0.0) -> None:
        self.embedder = embedder
        self.voices = voices
        self.match_threshold = match_threshold
        self.cluster_threshold = cluster_threshold
        self.min_seconds = min_seconds
        # Owner hint: a cluster within this margin below match_threshold of the owner's
        # voice, and at least owner_hint_db louder than every other cluster, is the owner.
        # A margin of 0 turns the hint off.
        self.owner_hint_margin = owner_hint_margin
        self.owner_hint_db = owner_hint_db
        self.absorb_threshold = absorb_threshold

    def diarize_runs(self, runs: list[np.ndarray], pieces: list[PieceRef]
                     ) -> tuple[list[SpeakerLabel], dict[str, np.ndarray]]:
        """Label each piece of the given audio runs; see ``diarize`` for the return value."""
        clips = [window(runs[p.run], p.start, p.end) for p in pieces]
        loud = [rms_db(runs[p.run][int(p.start * SAMPLE_RATE): int(p.end * SAMPLE_RATE)])
                for p in pieces]
        # pieces of different runs are far apart in time; offset runs so "nearest piece"
        # fallbacks stay within a run
        offset, starts = 0.0, []
        for r in runs:
            starts.append(offset)
            offset += r.size / SAMPLE_RATE + 3600
        times = [starts[p.run] + (p.start + p.end) / 2 for p in pieces]
        return self.diarize(clips, times, loud)

    def label(self, clips: list[np.ndarray], times: list[float]) -> list[SpeakerLabel]:
        return self.diarize(clips, times)[0]

    def diarize(self, clips: list[np.ndarray], times: list[float],
                loudness: list[float] | None = None
                ) -> tuple[list[SpeakerLabel], dict[str, np.ndarray]]:
        """Label each clip; ``times`` (clip midpoints) are used for short-clip fallback.

        Also returns the voice centroid of every *unnamed* speaker ("Speaker N"), so the
        speaker can be named later and recognised from then on.
        """
        if not clips:
            return [], {}
        min_len = int(self.min_seconds * SAMPLE_RATE)
        idx = [i for i, c in enumerate(clips) if c.size >= min_len]
        if not idx:
            # nothing long enough on its own: treat all clips as one speaker
            joint = np.concatenate(clips)
            label = SpeakerLabel("Speaker 1", False)
            unnamed: dict[str, np.ndarray] = {}
            if joint.size >= min_len:
                emb = self.embedder.embed(joint)
                (label,), unnamed = self.name_clusters([emb])
            return [label] * len(clips), unnamed
        embs = np.stack([self.embedder.embed(clips[i]) for i in idx])
        cids = absorb_singletons(embs, cluster(embs, self.cluster_threshold),
                                 self.absorb_threshold)
        n = max(cids) + 1
        centroids = [_normalize(embs[[j for j, c in enumerate(cids) if c == cid]].mean(axis=0))
                     for cid in range(n)]
        loud = None
        if loudness is not None:
            loud = [float(np.mean([loudness[idx[j]] for j, c in enumerate(cids) if c == cid]))
                    for cid in range(n)]
        names, unnamed = self.name_clusters(centroids, loud)
        out: list[SpeakerLabel | None] = [None] * len(clips)
        for j, i in enumerate(idx):
            out[i] = names[cids[j]]
        t = np.asarray(times)
        idx_arr = np.asarray(idx)
        for i in range(len(clips)):
            if out[i] is None:
                nearest = idx_arr[np.argmin(np.abs(t[idx_arr] - t[i]))]
                out[i] = out[int(nearest)]
        return [o for o in out if o is not None], unnamed

    def check_lines(self, runs: list[np.ndarray], pieces: list[PieceRef],
                    labels: list[SpeakerLabel], floor: float = 0.0) -> list[SpeakerLabel]:
        """Line voice check (``CONVO_SPEAKER_LINE_CHECK``): a line named after an enrolled
        person whose own voice (a >= 3 s window around it) fits another enrolled person
        at least as well, or fits its own name below ``floor``
        (``CONVO_SPEAKER_LINE_FLOOR``, 0 = off: someone new), loses the name and becomes
        ``UNSURE_LABEL``.

        A speaker's lines can include someone else's short turns (the diarizer missed
        them); those are the wrong names this catches. Moving the line to the other
        person measured worse than unnaming it: the better fit is often someone who
        isn't enrolled. The comparison needs two or more enrolled voices; the floor works
        with one.
        """
        by_name = {v.name: v for v in self.voices}
        if len(by_name) < 2 and floor <= 0:
            return list(labels)
        out = []
        for p, lab in zip(pieces, labels, strict=True):
            own = by_name.get(lab.name)
            if own is None:
                out.append(lab)
                continue
            e = self.embedder.embed(window(runs[p.run], p.start, p.end))
            mine = own.score(e)
            if mine < floor or any(v.score(e) >= mine for n, v in by_name.items()
                                   if n != lab.name):
                out.append(SpeakerLabel(UNSURE_LABEL, False))
            else:
                out.append(lab)
        return out

    def split_lines(self, runs: list[np.ndarray], pieces: list[PieceRef],
                    words: list[list[Word]], labels: list[SpeakerLabel]
                    ) -> list[list[tuple[int, int, SpeakerLabel]]]:
        """Parts of each line by speaker: (first word, end word, label). ECAPA keeps every
        line whole (splitting by voice measured worse in eval/recognition.py); the
        pyannote backend splits at its turn changes."""
        return [[(0, len(ws), lab)] for ws, lab in zip(words, labels, strict=True)]

    def name_clusters(self, centroids: list[np.ndarray], loudness: list[float] | None = None,
                      seconds: list[float] | None = None
                      ) -> tuple[list[SpeakerLabel], dict[str, np.ndarray]]:
        """Name each cluster (in order of first appearance) from the enrolled voices.

        ``seconds``: each cluster's amount of speech, for ``name_min_seconds``.
        """
        scores = np.array([[v.score(c) for v in self.voices] for c in centroids],
                          dtype=np.float32).reshape(len(centroids), len(self.voices))
        names: dict[int, SpeakerLabel] = {}
        blocked = self._unnameable(scores, seconds)
        # best pairs first, so a weak early cluster can't take a voice a later one fits better
        pairs = sorted(((float(scores[c, v]), c, v) for c in range(len(centroids))
                        for v in range(len(self.voices))), reverse=True)
        used: set[int] = set()
        for s, c, v in pairs:
            if s < self.match_threshold:
                break
            if c in names or v in used or c in blocked:
                continue
            names[c] = SpeakerLabel(self.voices[v].name, self.voices[v].is_owner)
            used.add(v)
        owner = self._owner_hint(scores, names, used, loudness)
        if owner is not None and owner[0] in blocked:
            owner = None
        if owner is not None:
            c, v = owner
            names[c] = SpeakerLabel(self.voices[v].name, True)
        out: list[SpeakerLabel] = []
        unnamed: dict[str, np.ndarray] = {}
        unknown = 0
        for c in range(len(centroids)):
            if c not in names:
                unknown += 1
                names[c] = SpeakerLabel(f"Speaker {unknown}", False)
                unnamed[names[c].name] = centroids[c]
            out.append(names[c])
        return out, unnamed

    def _unnameable(self, scores: np.ndarray, seconds: list[float] | None) -> set[int]:
        """Clusters the opt-in naming rules keep unnamed: too little speech, or a best
        voice that doesn't beat the runner-up voice by ``match_margin`` (two people fit
        about as well: likely neither, e.g. someone new)."""
        out: set[int] = set()
        for c in range(scores.shape[0]):
            if (self.name_min_seconds > 0 and seconds is not None
                    and seconds[c] < self.name_min_seconds):
                out.add(c)
            elif self.match_margin > 0 and scores.shape[1] >= 2:
                top2 = np.sort(scores[c])[-2:]
                if top2[1] - top2[0] < self.match_margin:
                    out.add(c)
        return out

    def _owner_hint(self, scores: np.ndarray, names: dict[int, SpeakerLabel], used: set[int],
                    loudness: list[float] | None) -> tuple[int, int] | None:
        owner = next((v for v, voice in enumerate(self.voices) if voice.is_owner), None)
        if (owner is None or owner in used or loudness is None or len(loudness) < 2
                or self.owner_hint_margin <= 0):
            return None
        loudest = int(np.argmax(loudness))
        others = [x for i, x in enumerate(loudness) if i != loudest]
        if loudest in names or loudness[loudest] - max(others) < self.owner_hint_db:
            return None
        if scores[loudest, owner] < self.match_threshold - self.owner_hint_margin:
            return None
        # not if the cluster looks more like someone else than like the owner
        if int(np.argmax(scores[loudest])) != owner:
            return None
        log.info("owner hint: speaker %d of %d named %s (voice score %.2f, below the match "
                 "threshold %.2f; %.1f dB louder than the next speaker)", loudest + 1,
                 len(loudness), self.voices[owner].name, float(scores[loudest, owner]),
                 self.match_threshold, loudness[loudest] - max(others))
        return loudest, owner


def load_voices(store: Any) -> list[EnrolledVoice]:
    """Enrolled voices from ``Store.speakers()``."""
    return [EnrolledVoice(s["name"], s["embedding"], s["is_owner"],
                          s.get("samples", np.zeros((0, 0), np.float32)))
            for s in store.speakers()]


# Enrollment audio is cut into samples of this length, so a profile keeps several
# voice samples (different words, pitch, loudness) instead of one average.
ENROLL_SAMPLE_SECONDS = 9.0


def enroll_voice(store: Any, embedder: SpeakerEmbedder, name: str, audio: np.ndarray,
                 is_owner: bool = False) -> int:
    """Store a voice profile for ``name`` from an enrollment recording (speech only).

    Replaces the person's earlier enrollment samples (samples learned from named
    speakers are kept). Returns the number of samples stored.
    """
    samples = enrollment_samples(embedder, audio)
    store.put_speaker(name, _normalize(np.mean(samples, axis=0)), is_owner,
                      samples=samples, source="enrolled")
    return len(samples)


def enrollment_samples(embedder: SpeakerEmbedder, audio: np.ndarray,
                       window_seconds: float = DIARIZATION_WINDOW,
                       sample_seconds: float = ENROLL_SAMPLE_SECONDS) -> list[np.ndarray]:
    """One sample per ``sample_seconds`` of speech (mean of 3 s window embeddings)."""
    win = int(window_seconds * SAMPLE_RATE)
    if audio.size < win:
        raise ValueError("enrollment audio must contain at least a few seconds of speech")
    vecs = [embedder.embed(audio[i:i + win]) for i in range(0, audio.size - win + 1, win)]
    per = max(1, int(round(sample_seconds / window_seconds)))
    return [_normalize(np.mean(vecs[i:i + per], axis=0)) for i in range(0, len(vecs), per)]


def enrollment_embedding(embedder: SpeakerEmbedder, audio: np.ndarray,
                         window_seconds: float = DIARIZATION_WINDOW) -> np.ndarray:
    """Average embedding over windows of an enrollment recording (>= ~20 s of speech)."""
    return _normalize(np.mean(enrollment_samples(embedder, audio, window_seconds), axis=0))
