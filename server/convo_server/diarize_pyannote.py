"""Diarization with pyannote.audio (``CONVO_DIARIZER=pyannote``, extra ``pyannote``).

pyannote finds who spoke when on the whole recording (overlap-aware segmentation and
clustering), which is better than clustering one embedding per whisper segment. The
default pipeline is ``pyannote/speaker-diarization-community-1`` (code MIT, weights
CC-BY-4.0) from the ungated ``pyannote-community`` copy on Hugging Face. It runs
locally; the weights are downloaded once into ``CONVO_MODELS_DIR`` (or point
``CONVO_PYANNOTE_MODEL`` at a local directory). The pyannoteAI cloud API is never used:
cloud pipeline names are refused.

Enrolled voices stay ECAPA voiceprints: each pyannote speaker gets an ECAPA embedding
from its own speech and is named by ``Diarizer.name_clusters`` exactly like the default
backend. Speakers of separate runs (gaps in a session) are merged by clustering those
embeddings.

pyannote.audio 4 sends usage telemetry to pyannote.ai by default. This module turns it
off before pyannote is imported; nothing in the data path may leave the network.
"""

from __future__ import annotations

import os

# Must be set before pyannote.audio is imported anywhere (it reads it at import time).
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
# huggingface_hub (model download) usage pings off too
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import logging  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from .audio import SAMPLE_RATE  # noqa: E402
from .diarize import (  # noqa: E402
    Diarizer,
    EnrolledVoice,
    PieceRef,
    SpeakerEmbedder,
    SpeakerLabel,
    _normalize,
    cluster,
    rms_db,
    window,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "pyannote-community/speaker-diarization-community-1"
# Up to this much of a speaker's own speech is embedded for their voiceprint.
MAX_SPEAKER_SECONDS = 60.0

Turn = tuple[float, float, str]  # start, end (seconds in the run), pyannote label


def _refuse_cloud(model: str) -> None:
    lowered = model.lower()
    if "precision" in lowered or lowered.endswith("-cloud") or "pyannoteai" in lowered:
        raise ValueError(f"{model!r} is a pyannoteAI cloud pipeline; only local pipelines "
                         "are allowed (audio must not leave the network)")


class PyannoteSegmenter:
    """Runs a local pyannote pipeline on 16 kHz mono audio; returns exclusive turns."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Path | None = None,
                 device: str = "auto", threads: int | None = None) -> None:
        _refuse_cloud(model)
        try:
            import torch
            from pyannote.audio import Pipeline
            from pyannote.audio.telemetry import set_telemetry_metrics
        except ImportError as e:  # pragma: no cover - optional extra
            raise RuntimeError(
                "CONVO_DIARIZER=pyannote needs the 'pyannote' extra: "
                "pip install -e '.[diarization,pyannote]'") from e
        set_telemetry_metrics(False)
        self._torch = torch
        if threads:
            torch.set_num_threads(threads)
        self._pipeline = Pipeline.from_pretrained(  # pragma: no cover - needs weights
            model, cache_dir=str(cache_dir) if cache_dir else None)
        if self._pipeline is None:  # pragma: no cover
            raise RuntimeError(f"could not load pyannote pipeline {model!r}")
        if device == "auto":  # pragma: no cover - depends on the machine
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._pipeline.to(torch.device(device))  # pragma: no cover

    def __call__(self, audio: np.ndarray) -> list[Turn]:  # pragma: no cover - needs model
        wav = self._torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
        out = self._pipeline({"waveform": wav, "sample_rate": SAMPLE_RATE})
        ann = getattr(out, "exclusive_speaker_diarization", out)
        return [(float(seg.start), float(seg.end), str(label))
                for seg, _, label in ann.itertracks(yield_label=True)]


class PyannoteDiarizer(Diarizer):
    def __init__(self, embedder: SpeakerEmbedder, voices: list[EnrolledVoice],
                 segmenter: Callable[[np.ndarray], list[Turn]],
                 match_threshold: float = 0.50, cluster_threshold: float = 0.45,
                 min_seconds: float = 1.0, owner_hint_margin: float = 0.10,
                 owner_hint_db: float = 3.0, absorb_threshold: float = 0.0,
                 match_margin: float = 0.05) -> None:
        super().__init__(embedder, voices, match_threshold, cluster_threshold, min_seconds,
                         owner_hint_margin, owner_hint_db, absorb_threshold, match_margin)
        self.segmenter = segmenter

    def diarize_runs(self, runs: list[np.ndarray], pieces: list[PieceRef]
                     ) -> tuple[list[SpeakerLabel], dict[str, np.ndarray]]:
        if not pieces:
            return [], {}
        sr = SAMPLE_RATE
        turns = [self.segmenter(r) if r.size else [] for r in runs]
        # one ECAPA embedding + loudness per (run, pyannote speaker) with enough speech
        keys: list[tuple[int, str]] = []
        embs: list[np.ndarray] = []
        loud: list[float] = []
        for ri, (audio, tt) in enumerate(zip(runs, turns, strict=True)):
            for spk in sorted({t[2] for t in tt}, key=lambda s: min(t[0] for t in tt
                                                                    if t[2] == s)):
                own = [audio[int(a * sr): int(b * sr)] for a, b, s in tt if s == spk]
                speech = np.concatenate(own) if own else np.zeros(0, np.float32)
                speech = speech[: int(MAX_SPEAKER_SECONDS * sr)]
                if speech.size < int(self.min_seconds * sr):
                    continue
                keys.append((ri, spk))
                embs.append(self._embed_speech(speech))
                loud.append(rms_db(speech))
        if not keys:  # pyannote heard nobody long enough: per-piece fallback
            return super().diarize_runs(runs, pieces)
        # pyannote already decided who is who inside a run, so its speakers of one run are
        # never merged; only speakers of different runs are joined by voice. (Merging
        # within a run collapsed two people on a body-worn mic into one name: their ECAPA
        # voiceprints are often more similar than the cluster threshold.)
        cids = (cluster(np.stack(embs), self.cluster_threshold,
                        cannot_link=[ri for ri, _ in keys]) if len(embs) > 1 else [0])
        log.info("pyannote: %s speaker(s) per run, %d after joining runs",
                 [len({t[2] for t in tt}) for tt in turns], max(cids) + 1)
        n = max(cids) + 1
        members = [[j for j, c in enumerate(cids) if c == cid] for cid in range(n)]
        centroids = [_normalize(np.mean([embs[j] for j in m], axis=0)) for m in members]
        cl_loud = [float(np.mean([loud[j] for j in m])) for m in members]
        names, unnamed = self.name_clusters(centroids, cl_loud)
        by_key = {k: names[cids[j]] for j, k in enumerate(keys)}

        out: list[SpeakerLabel] = []
        for p in pieces:
            label = _best_turn(turns[p.run], p.start, p.end)
            out.append(by_key.get((p.run, label)) if label else None)  # type: ignore[arg-type]
        # pieces without a usable turn take the nearest labelled piece of the same run
        for i, p in enumerate(pieces):
            if out[i] is None:
                near = [j for j, q in enumerate(pieces) if q.run == p.run and out[j] is not None]
                if near:
                    j = min(near, key=lambda j: abs((pieces[j].start + pieces[j].end) / 2
                                                     - (p.start + p.end) / 2))
                    out[i] = out[j]
        if any(o is None for o in out):  # a run where nobody could be identified
            rest = [i for i, o in enumerate(out) if o is None]
            labels, extra = self.diarize([window(runs[pieces[i].run], pieces[i].start,
                                                 pieces[i].end) for i in rest],
                                         [pieces[i].start for i in rest])
            # its "Speaker N" numbering starts again at 1: continue after ours instead
            renumber = {name: f"Speaker {len(unnamed) + k}"
                        for k, name in enumerate(extra, 1)}
            for name, emb in extra.items():
                unnamed[renumber[name]] = emb
            for i, lab in zip(rest, labels, strict=True):
                out[i] = SpeakerLabel(renumber.get(lab.name, lab.name), lab.is_owner)
        return out, unnamed

    def _embed_speech(self, speech: np.ndarray, win_seconds: float = 3.0) -> np.ndarray:
        win = int(win_seconds * SAMPLE_RATE)
        if speech.size <= win:
            return _normalize(self.embedder.embed(speech))
        vecs = [self.embedder.embed(speech[i:i + win])
                for i in range(0, speech.size - win // 2, win)]
        return _normalize(np.mean(vecs, axis=0))


def _best_turn(turns: list[Turn], start: float, end: float) -> str | None:
    """Label of the turn overlapping [start, end] most (nearest turn if none overlaps)."""
    if not turns:
        return None
    overlap: dict[str, float] = {}
    for a, b, s in turns:
        o = min(b, end) - max(a, start)
        if o > 0:
            overlap[s] = overlap.get(s, 0.0) + o
    if overlap:
        return max(overlap, key=lambda s: overlap[s])
    mid = (start + end) / 2
    return min(turns, key=lambda t: min(abs(t[0] - mid), abs(t[1] - mid)))[2]
