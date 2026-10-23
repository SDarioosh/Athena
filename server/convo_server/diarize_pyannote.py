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
from typing import Any  # noqa: E402

import numpy as np  # noqa: E402

from .audio import SAMPLE_RATE  # noqa: E402
from .diarize import (  # noqa: E402
    Diarizer,
    EnrolledVoice,
    PieceRef,
    SpeakerEmbedder,
    SpeakerLabel,
    Word,
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
                 device: str = "auto", threads: int | None = None, *,
                 cluster_threshold: float | None = None, min_speakers: int | None = None,
                 max_speakers: int | None = None, vbx_fa: float | None = None,
                 vbx_fb: float | None = None) -> None:
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
        tune_clustering(self._pipeline, cluster_threshold, vbx_fa, vbx_fb)  # pragma: no cover
        self._speakers = speaker_bounds(min_speakers, max_speakers)
        if device == "auto":  # pragma: no cover - depends on the machine
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._pipeline.to(torch.device(device))  # pragma: no cover

    def __call__(self, audio: np.ndarray) -> list[Turn]:  # pragma: no cover - needs model
        wav = self._torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
        out = self._pipeline({"waveform": wav, "sample_rate": SAMPLE_RATE}, **self._speakers)
        ann = getattr(out, "exclusive_speaker_diarization", out)
        return [(float(seg.start), float(seg.end), str(label))
                for seg, _, label in ann.itertracks(yield_label=True)]


class PyannoteEmbedder:
    """Speaker embeddings from the loaded pyannote pipeline's own model (WeSpeaker
    ResNet34 for community-1, 256 values; ``CONVO_SPEAKER_EMBEDDER=pyannote``) instead of
    ECAPA. Voices enrolled or named with ECAPA don't match it: enroll again after
    switching (see ``EnrolledVoice.score``)."""

    def __init__(self, segmenter: PyannoteSegmenter) -> None:  # pragma: no cover - model
        self._torch = segmenter._torch
        self._model = segmenter._pipeline._embedding
        self._min = int(getattr(self._model, "min_num_samples", 400))

    def embed(self, audio: np.ndarray) -> np.ndarray:  # pragma: no cover - needs model
        x = np.ascontiguousarray(audio, dtype=np.float32)
        if x.size < self._min:
            x = np.pad(x, (0, self._min - x.size))
        v = np.asarray(self._model(self._torch.from_numpy(x)[None, None]))[0]
        return _normalize(np.nan_to_num(v))


def tune_clustering(pipeline: Any, threshold: float | None, fa: float | None = None,
                    fb: float | None = None) -> None:
    """Set the pipeline's clustering parameters; None keeps the pipeline's own.

    ``threshold`` (``CONVO_PYANNOTE_CLUSTER_THRESHOLD``, 0.6 in community-1) only seeds
    VBx clustering; how many speakers come out is decided by VBx's ``Fa`` (0.07) and
    ``Fb`` (0.8) (``CONVO_PYANNOTE_VBX_FA``/``_FB``): a lower Fb or a higher Fa keeps
    more speakers apart.
    """
    wanted = {k: float(v) for k, v in (("threshold", threshold), ("Fa", fa), ("Fb", fb))
              if v is not None}
    if not wanted:
        return
    params = pipeline.parameters(instantiated=True)
    missing = [k for k in wanted if k not in params.get("clustering", {})]
    if missing:
        raise ValueError(f"this pyannote pipeline has no clustering {', '.join(missing)}")
    params["clustering"].update(wanted)
    pipeline.instantiate(params)


def speaker_bounds(min_speakers: int | None, max_speakers: int | None) -> dict[str, int]:
    """Keyword arguments for the pipeline call: speaker count bounds, if set (> 0)."""
    out = {}
    if min_speakers:
        out["min_speakers"] = int(min_speakers)
    if max_speakers:
        out["max_speakers"] = int(max_speakers)
    if "min_speakers" in out and "max_speakers" in out and out["min_speakers"] > out[
            "max_speakers"]:
        raise ValueError("CONVO_PYANNOTE_MIN_SPEAKERS is above CONVO_PYANNOTE_MAX_SPEAKERS")
    return out


class PyannoteDiarizer(Diarizer):
    def __init__(self, embedder: SpeakerEmbedder, voices: list[EnrolledVoice],
                 segmenter: Callable[[np.ndarray], list[Turn]],
                 match_threshold: float = 0.50, cluster_threshold: float = 0.45,
                 min_seconds: float = 1.0, owner_hint_margin: float = 0.10,
                 owner_hint_db: float = 3.0, absorb_threshold: float = 0.0) -> None:
        super().__init__(embedder, voices, match_threshold, cluster_threshold, min_seconds,
                         owner_hint_margin, owner_hint_db, absorb_threshold)
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
        secs: list[float] = []
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
                secs.append(sum(b - a for a, b, s in tt if s == spk))
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
        cl_secs = [float(sum(secs[j] for j in m)) for m in members]
        names, unnamed = self.name_clusters(centroids, cl_loud, cl_secs)
        by_key = {k: names[cids[j]] for j, k in enumerate(keys)}
        self._last_turns, self._last_by_key = turns, by_key

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

    def split_lines(self, runs: list[np.ndarray], pieces: list[PieceRef],
                    words: list[list[Word]], labels: list[SpeakerLabel]
                    ) -> list[list[tuple[int, int, SpeakerLabel]]]:
        """Split lines at pyannote's turn changes: each word takes the speaker of the
        turn it overlaps most; a part shorter than ``split_min_seconds`` joins the part
        before it (or after it, at the start)."""
        turns = getattr(self, "_last_turns", None)
        by_key = getattr(self, "_last_by_key", {})
        out = []
        for p, ws, lab in zip(pieces, words, labels, strict=True):
            if not turns or len(ws) < 2 or p.run >= len(turns):
                out.append([(0, len(ws), lab)])
                continue
            who = []
            for s, e, _ in ws:
                t = _best_turn(turns[p.run], s, max(e, s + 1e-3))
                who.append(by_key.get((p.run, t), lab) if t else lab)
            parts: list[list] = []  # [first, end, label]
            for k, w in enumerate(who):
                if parts and parts[-1][2] == w:
                    parts[-1][1] = k + 1
                else:
                    parts.append([k, k + 1, w])
            merged: list[list] = []
            for part in parts:
                long = ws[part[1] - 1][1] - ws[part[0]][0] >= self.split_min_seconds
                if merged and (not long or merged[-1][2] == part[2]):
                    merged[-1][1] = part[1]
                elif (merged and ws[merged[-1][1] - 1][1] - ws[merged[-1][0]][0]
                      < self.split_min_seconds):
                    merged[-1][1], merged[-1][2] = part[1], part[2]
                else:
                    merged.append(list(part))
            out.append([(a, b, w) for a, b, w in merged])
        return out

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
