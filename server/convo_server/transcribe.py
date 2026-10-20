"""whisper.cpp transcription backends.

Two ways to run whisper.cpp locally:

* ``WhisperServerTranscriber`` talks to a running ``whisper-server`` (model stays
  loaded, preferred).
* ``WhisperCliTranscriber`` shells out to ``whisper-cli`` per request.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from .audio import to_wav_bytes


@dataclass(frozen=True)
class TranscriptPiece:
    start: float  # seconds relative to the audio passed in
    end: float
    text: str
    # (start, end, text) per token when the backend reports them (verbose_json); the
    # pipeline uses them to split a piece where a clip joins two cut-apart regions.
    words: tuple[tuple[float, float, str], ...] = ()


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        """``prompt``: vocabulary and names that help whisper spell them right."""
        ...


# whisper.cpp marks non-speech as a bracketed or starred tag: [BLANK_AUDIO], [MUSIC],
# (silence), (upbeat music), *laughs*. A piece that is only such tags is not words.
_NON_SPEECH = re.compile(r"^(\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*)+$")


_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _clean(pieces: list[TranscriptPiece]) -> list[TranscriptPiece]:
    out = []
    for p in pieces:
        text = p.text.strip()
        # whisper also emits pieces that are only punctuation (".", "...", "-")
        if not text or _NON_SPEECH.match(text) or not _WORD.search(text):
            continue
        out.append(TranscriptPiece(p.start, max(p.end, p.start), text, p.words))
    return out


# ------------------------------------------------------------------ hallucinations
@dataclass(frozen=True)
class HallucinationFilter:
    """Drops text whisper invents on noise or silence (CONVO_WHISPER_DROP_*).

    Uses the per-token probabilities of whisper-server's ``verbose_json`` (and
    ``whisper-cli -ojf``). Measured in the README's "Recognition eval" section.
    """

    enabled: bool = True
    # A segment that is only a stock phrase ("Thank you.", "Bye.") is dropped when the
    # mean log-probability of its tokens is below this. People do say "thank you": heard,
    # it scored -0.02 to -0.16 (one far-field one -0.66); invented on 2 s of room noise,
    # -0.63 to -0.90 (turbo). -0.7 keeps every real one measured, so it drops about half
    # of the invented ones: deleting a real word costs as much as an invented one.
    phrase_logprob: float = -0.7
    # A word or phrase repeated more than this many times in a row is cut back to this
    # many, and so are runs of identical segments (whisper's repetition loops). 0 = off.
    max_repeats: int = 4


# What whisper says on silence and noise (it learned them from subtitle endings),
# compared lowercased without punctuation.
STOCK_PHRASES = frozenset({
    "you", "thank you", "thank you very much", "thank you so much", "thanks",
    "thanks for watching", "thank you for watching", "thanks for listening",
    "thank you for listening", "bye", "bye bye", "goodbye", "amen", "please subscribe",
    "see you next time", "the end",
})
_SUBTITLE_CREDIT = re.compile(
    r"\b(subtitles?|captions?|captioning|transcript(ion)?)\s+(by|from)\b|\bamara\.org\b",
    re.IGNORECASE)


def _plain(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def mean_logprob(seg: dict) -> float | None:
    """Mean log-probability of a segment's text tokens (``words[].probability``)."""
    probs = [float(w["probability"]) for w in seg.get("words") or []
             if str(w.get("word", "")).strip() and w.get("probability") is not None]
    if not probs:
        return None
    return sum(math.log(max(p, 1e-6)) for p in probs) / len(probs)


def collapse_repeats(text: str, max_repeats: int) -> str:
    """Cut a word or phrase (up to 8 words) repeated > ``max_repeats`` times in a row."""
    if max_repeats < 1:
        return text
    tokens = text.split()
    keys = [_plain(t) for t in tokens]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        cut = reps = 0
        for n in range(1, 9):
            unit = keys[i:i + n]
            if len(unit) < n or not all(unit):
                break
            reps = 1
            while keys[i + reps * n: i + (reps + 1) * n] == unit:
                reps += 1
            if reps > max_repeats:
                cut = n
                break
        if cut:
            out += tokens[i:i + cut * max_repeats]
            i += cut * reps
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def is_hallucination(seg: dict, f: HallucinationFilter) -> bool:
    """True when a whisper segment looks invented rather than heard."""
    text = str(seg.get("text", ""))
    if _SUBTITLE_CREDIT.search(text):
        return True
    if _plain(text) in STOCK_PHRASES:
        lp = mean_logprob(seg)
        return lp is not None and lp < f.phrase_logprob
    return False


def filter_segments(segs: list[dict], f: HallucinationFilter | None) -> list[dict]:
    """Drop invented segments and cut repetition loops, within and across segments."""
    if f is None or not f.enabled:
        return segs
    out: list[dict] = []
    run = 0
    for seg in segs:
        if is_hallucination(seg, f):
            continue
        text = collapse_repeats(str(seg.get("text", "")), f.max_repeats)
        same = bool(out) and _plain(text) == _plain(str(out[-1]["text"])) and _plain(text)
        run = run + 1 if same else 1
        if f.max_repeats >= 1 and run > f.max_repeats:
            continue
        # A rewritten text no longer matches its word times; without them the piece is
        # never re-split from words (clips.Window.split), so the cut loop stays cut.
        same_words = text.split() == str(seg.get("text", "")).split()
        out.append({**seg, "text": text} if same_words
                   else {**seg, "text": text, "words": None})
    return out


class WhisperServerTranscriber:
    # Set from Settings by cli.build_transcriber; None keeps every segment.
    hallucinations: HallucinationFilter | None = HallucinationFilter()

    def __init__(self, base_url: str, language: str = "en", timeout: float = 600.0,
                 temperature_inc: float = 0.2,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.url = base_url.rstrip("/") + "/inference"
        self.language = language
        self.timeout = timeout
        # Decode at temperature 0; when a result looks like a failure (repetition, low
        # log-probability) whisper retries with the temperature raised by this step.
        self.temperature_inc = temperature_inc
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        data = {"response_format": "verbose_json", "language": self.language,
                "temperature": "0.0", "temperature_inc": str(self.temperature_inc)}
        if prompt:
            data["prompt"] = prompt
        resp = self._http.post(
            self.url,
            files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
            data=data,
        )
        resp.raise_for_status()
        return parse_verbose_json(resp.json(), self.hallucinations)


def parse_verbose_json(payload: dict,
                       hallucinations: HallucinationFilter | None = None
                       ) -> list[TranscriptPiece]:
    segs = payload.get("segments")
    if segs is None:  # plain {"text": ...} response
        text = payload.get("text", "")
        return _clean([TranscriptPiece(0.0, 0.0, text)]) if text.strip() else []
    return _clean([TranscriptPiece(float(s["start"]), float(s["end"]), s["text"],
                                   _words(s)) for s in filter_segments(segs, hallucinations)])


def _words(seg: dict) -> tuple[tuple[float, float, str], ...]:
    return tuple((float(w["start"]), float(w["end"]), w["word"])
                 for w in seg.get("words") or () if "start" in w and "end" in w)


class WhisperCliTranscriber:
    hallucinations: HallucinationFilter | None = HallucinationFilter()  # see above

    def __init__(self, binary: str, model: str, language: str = "en", threads: int = 4,
                 temperature_inc: float = 0.2) -> None:
        self.binary = binary
        self.model = model
        self.language = language
        self.threads = threads
        self.temperature_inc = temperature_inc

    def command(self, wav: Path, out_base: Path, prompt: str = "") -> list[str]:
        cmd = [self.binary, "-m", self.model, "-f", str(wav), "-l", self.language,
               "-t", str(self.threads), "-tp", "0.0", "-tpi", str(self.temperature_inc),
               "-ojf", "-of", str(out_base), "-np"]
        return cmd + (["--prompt", prompt] if prompt else [])

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        with tempfile.TemporaryDirectory(prefix="convo-whisper-") as tmp:
            wav = Path(tmp) / "audio.wav"
            wav.write_bytes(to_wav_bytes(audio))
            out_base = Path(tmp) / "out"
            subprocess.run(self.command(wav, out_base, prompt), check=True,
                           capture_output=True)
            return parse_cli_json(json.loads(out_base.with_suffix(".json").read_text()),
                                  self.hallucinations)


_SPECIAL_TOKEN = re.compile(r"^\[_[A-Z]+_?\d*\]$")  # [_BEG_], [_TT_550]


def parse_cli_json(payload: dict,
                   hallucinations: HallucinationFilter | None = None
                   ) -> list[TranscriptPiece]:
    """``whisper-cli -oj`` output; with ``-ojf`` it also has per-token probabilities."""
    segs = [{"start": s["offsets"]["from"] / 1000.0, "end": s["offsets"]["to"] / 1000.0,
             "text": s["text"],
             "words": [{"word": t.get("text", ""), "probability": t.get("p")}
                       for t in s.get("tokens", [])
                       if not _SPECIAL_TOKEN.match(str(t.get("text", "")))]}
            for s in payload.get("transcription", [])]
    return _clean([TranscriptPiece(s["start"], s["end"], s["text"])
                   for s in filter_segments(segs, hallucinations)])
