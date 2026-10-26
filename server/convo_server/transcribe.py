"""Local transcription backends.

Two ways to run whisper.cpp locally:

* ``WhisperServerTranscriber`` talks to a running ``whisper-server`` (model stays
  loaded, preferred).
* ``WhisperCliTranscriber`` shells out to ``whisper-cli`` per request.

And one alternative model, for an A/B against whisper on the eval:

* ``ParakeetTranscriber`` runs NVIDIA Parakeet TDT 0.6B v3 in-process with
  sherpa-onnx (extra ``parakeet``), on CPU.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
    # A sentence (SENTENCE_WORDS or more different words) repeated back to back is kept
    # once: people repeat "yeah" or "no, no", but whisper is the one that says a whole
    # sentence again (it fills a later pause or someone else's turn with it).
    max_repeats: int = 4


# Different words that make a repeated phrase or segment a sentence (see max_repeats).
SENTENCE_WORDS = 4


def repeat_limit(words: list[str], max_repeats: int) -> int:
    """How many times in a row ``words`` (plain, lowercased) may stand."""
    return 1 if len(set(words)) >= SENTENCE_WORDS else max_repeats


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
            limit = repeat_limit(unit, max_repeats)
            if reps > limit:
                cut = n
                break
        if cut:
            out += tokens[i:i + cut * limit]
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
        plain = _plain(text)
        same = bool(out) and bool(plain) and plain == _plain(str(out[-1]["text"]))
        run = run + 1 if same else 1
        if f.max_repeats >= 1 and run > repeat_limit(plain.split(), f.max_repeats):
            continue
        # A rewritten text no longer matches its word times; without them the piece is
        # never re-split from words (clips.Window.split), so the cut loop stays cut.
        same_words = text.split() == str(seg.get("text", "")).split()
        out.append({**seg, "text": text} if same_words
                   else {**seg, "text": text, "words": None})
    return out


@dataclass(frozen=True)
class DecodeOptions:
    """whisper.cpp decoding settings, sent with every request (CONVO_WHISPER_*).

    The defaults are whisper.cpp's own, except where the recognition eval measured better.
    """

    # Decode at temperature 0; when a result looks like a failure (repetition, low
    # log-probability) whisper retries with the temperature raised by this step.
    temperature_inc: float = 0.2
    beam_size: int = -1  # > 1 = beam search, else greedy
    best_of: int = 2  # candidates sampled at temperature > 0 (fallback)
    no_speech_thold: float = 0.6
    logprob_thold: float = -1.0
    entropy_thold: float = 2.4
    suppress_non_speech: bool = True
    carry_initial_prompt: bool = False

    def form_fields(self) -> dict[str, str]:
        """Multipart fields for whisper-server's /inference."""
        return {"temperature": "0.0", "temperature_inc": str(self.temperature_inc),
                "beam_size": str(self.beam_size), "best_of": str(self.best_of),
                "no_speech_thold": str(self.no_speech_thold),
                "logprob_thold": str(self.logprob_thold),
                "entropy_thold": str(self.entropy_thold),
                "suppress_non_speech": _bool(self.suppress_non_speech),
                "carry_initial_prompt": _bool(self.carry_initial_prompt)}

    def cli_args(self) -> list[str]:
        """The same settings as whisper-cli flags."""
        args = ["-tp", "0.0", "-tpi", str(self.temperature_inc),
                "-bs", str(self.beam_size), "-bo", str(self.best_of),
                "-nth", str(self.no_speech_thold), "-lpt", str(self.logprob_thold),
                "-et", str(self.entropy_thold)]
        if self.suppress_non_speech:
            args.append("-sns")
        if self.carry_initial_prompt:
            args.append("--carry-initial-prompt")
        return args


def _bool(v: bool) -> str:
    return "true" if v else "false"


class WhisperServerTranscriber:
    # Set from Settings by cli.build_transcriber; None keeps every segment.
    hallucinations: HallucinationFilter | None = HallucinationFilter()

    def __init__(self, base_url: str, language: str = "en", timeout: float = 600.0,
                 temperature_inc: float = 0.2,
                 transport: httpx.BaseTransport | None = None,
                 decode: DecodeOptions | None = None) -> None:
        self.url = base_url.rstrip("/") + "/inference"
        self.language = language
        self.timeout = timeout
        self.decode = decode or DecodeOptions(temperature_inc=temperature_inc)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        data = {"response_format": "verbose_json", "language": self.language,
                **self.decode.form_fields()}
        if prompt:
            data["prompt"] = prompt
        wav = to_wav_bytes(audio)
        for attempt in (1, 2):
            try:
                resp = self._http.post(self.url, files={"file": ("audio.wav", wav, "audio/wav")},
                                       data=data)
                break
            except httpx.RemoteProtocolError:
                # whisper-server closed a kept-alive connection we reused: one retry on a
                # fresh connection, instead of failing the whole session.
                if attempt == 2:
                    raise
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
                 temperature_inc: float = 0.2, decode: DecodeOptions | None = None) -> None:
        self.binary = binary
        self.model = model
        self.language = language
        self.threads = threads
        self.decode = decode or DecodeOptions(temperature_inc=temperature_inc)

    def command(self, wav: Path, out_base: Path, prompt: str = "") -> list[str]:
        cmd = [self.binary, "-m", self.model, "-f", str(wav), "-l", self.language,
               "-t", str(self.threads), *self.decode.cli_args(),
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


# --- Parakeet TDT (sherpa-onnx) ---

_SAMPLE_RATE = 16000
_SENTENCE_END = (".", "?", "!", "…")


class ParakeetTranscriber:
    """NVIDIA Parakeet TDT 0.6B v3 through sherpa-onnx, in-process on CPU.

    ``model_dir`` is an unpacked sherpa-onnx export, e.g.
    ``sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`` from the k2-fsa ``asr-models``
    release (``encoder*.onnx``, ``decoder*.onnx``, ``joiner*.onnx``, ``tokens.txt``).

    Clips longer than ``max_chunk_seconds`` are cut at the quietest point between half
    and all of that length, so every piece the encoder sees starts in a pause (TDT can
    return nothing for audio that starts mid-word). Pieces are sentences built from the
    token timestamps, split at sentence punctuation or a pause > ``gap_seconds``, like
    whisper's segments.

    ``redecode_holes``: TDT sometimes stops emitting tokens for a stretch of real speech
    inside a chunk, then resumes. After decoding a chunk, every gap between tokens (or
    before the first / after the last) ≥ ``hole_seconds`` that holds ≥ ``hole_seconds``
    of speech energy (20 ms frames ≥ 10 dB above the chunk's 10th-percentile frame) is
    decoded again on its own (± 0.25 s), and the words that start inside the gap are
    merged in. One pass, no recursion.

    ``prompt`` is ignored: Parakeet has no prompt or vocabulary biasing. The language
    is detected by the model (25 European languages), so ``CONVO_LANGUAGE`` is not used.
    """

    def __init__(self, model_dir: str | Path, threads: int = 4, *,
                 max_chunk_seconds: float = 30.0, gap_seconds: float = 0.8,
                 redecode_holes: bool = False, hole_seconds: float = 1.0,
                 recognizer: Any = None) -> None:
        self.max_chunk_seconds = max_chunk_seconds
        self.gap_seconds = gap_seconds
        self.redecode_holes = redecode_holes
        self.hole_seconds = hole_seconds
        self._rec = recognizer if recognizer is not None else _load_parakeet(
            Path(model_dir), threads)

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        audio = np.asarray(audio, dtype=np.float32)
        pieces: list[TranscriptPiece] = []
        for a, b in split_points(audio, self.max_chunk_seconds):
            clip = audio[a:b]
            if clip.size < _SAMPLE_RATE // 10:  # < 100 ms: nothing to recognize
                continue
            res = self._decode(clip)
            if self.redecode_holes:
                res = self._fill_holes(clip, res)
            pieces += tokens_to_pieces(res, a / _SAMPLE_RATE, self.gap_seconds)
        return _clean(pieces)

    def _decode(self, clip: np.ndarray) -> Any:
        stream = self._rec.create_stream()
        stream.accept_waveform(_SAMPLE_RATE, clip)
        self._rec.decode_stream(stream)
        return stream.result

    def _fill_holes(self, clip: np.ndarray, res: Any) -> Any:
        """Re-decode speech holes in one chunk's result and merge their words in."""
        timed = _token_times(res)
        if timed is None or not timed[0]:
            return res  # no tokens: a re-decode would see the same clip
        tokens, starts, durs = timed
        length = clip.size / _SAMPLE_RATE
        ends = [s + max(d, 0.0) for s, d in zip(starts, durs, strict=True)]
        bounds = [0.0, *ends]
        nexts = [*starts, length]
        speech = _speech_frames(clip)
        frame_s = _FRAME / _SAMPLE_RATE
        own = [(s, t, d, False) for s, t, d in zip(starts, tokens, durs, strict=True)]
        own_words = _token_words(own)
        added: list[tuple[float, str, float, bool]] = []
        for lo, hi in zip(bounds, nexts, strict=True):
            if hi - lo < self.hole_seconds:
                continue
            f0, f1 = int(lo / frame_s), int(np.ceil(hi / frame_s))
            if speech[f0:f1].sum() * frame_s < self.hole_seconds:
                continue
            a = max(0, int((lo - _HOLE_PAD) * _SAMPLE_RATE))
            b = min(clip.size, int((hi + _HOLE_PAD) * _SAMPLE_RATE))
            if b - a >= clip.size:
                continue  # the whole chunk again: same input, same output
            sub = _token_times(self._decode(clip[a:b]))
            if sub is None:
                continue
            off = a / _SAMPLE_RATE
            # Edge words of the first decode, to drop if the re-decode repeats them.
            edge = {_norm(w) for w in own_words
                    if abs(w[-1][0] + w[-1][2] - lo) < _HOLE_PAD or abs(w[0][0] - hi) < _HOLE_PAD}
            for word in _token_words([(s + off, t, d, True) for t, s, d in zip(*sub, strict=True)]):
                start = word[0][0]
                if not lo <= start < hi:
                    continue
                if _norm(word) in edge and (start - lo < _HOLE_PAD or hi - start < _HOLE_PAD):
                    continue
                if not word[0][1].startswith(" "):  # don't glue onto the word before
                    word[0] = (word[0][0], " " + word[0][1], word[0][2], True)
                added += word
        if not added:
            return res
        merged = sorted(own + added, key=lambda x: x[0])
        if merged[0][1].startswith(" ") and merged[0][3]:
            merged[0] = (merged[0][0], merged[0][1].lstrip(), merged[0][2], True)
        toks = [m[1] for m in merged]
        return _Tokens(text="".join(toks).strip(), tokens=toks,
                       timestamps=[m[0] for m in merged], durations=[m[2] for m in merged])


_FRAME = _SAMPLE_RATE // 50  # 20 ms
_HOLE_PAD = 0.25  # seconds of context on each side of a re-decoded hole


@dataclass
class _Tokens:
    """A sherpa-onnx-like result after hole re-decoding."""

    text: str
    tokens: list[str]
    timestamps: list[float]
    durations: list[float]


def _speech_frames(clip: np.ndarray) -> np.ndarray:
    """Per 20 ms frame: energy ≥ 10 dB above the clip's 10th-percentile frame."""
    n = clip.size // _FRAME * _FRAME
    if n == 0:
        return np.zeros(0, dtype=bool)
    db = 10 * np.log10(np.square(clip[:n].reshape(-1, _FRAME)).mean(axis=1) + 1e-12)
    return db >= np.percentile(db, 10) + 10.0


def _token_words(tokens: list[tuple]) -> list[list[tuple]]:
    """Group (start, token, ...) tuples into words: a word starts at a leading space."""
    out: list[list[tuple]] = []
    for t in tokens:
        if not out or t[1].startswith(" "):
            out.append([t])
        else:
            out[-1].append(t)
    return out


def _norm(word: list[tuple]) -> str:
    return re.sub(r"[^\w]", "", "".join(t[1] for t in word).lower())


def _load_parakeet(model_dir: Path, threads: int) -> Any:
    try:
        import sherpa_onnx
    except ImportError as e:
        raise RuntimeError(
            "CONVO_TRANSCRIBER=parakeet needs sherpa-onnx: pip install -e '.[parakeet]'"
        ) from e

    def pick(stem: str) -> str:
        found = sorted(model_dir.glob(f"{stem}*.onnx"), key=lambda p: ".int8." not in p.name)
        if not found:
            raise RuntimeError(f"no {stem}*.onnx in {model_dir} (CONVO_PARAKEET_MODEL_DIR)")
        return str(found[0])

    tokens = model_dir / "tokens.txt"
    if not tokens.is_file():
        raise RuntimeError(f"no tokens.txt in {model_dir} (CONVO_PARAKEET_MODEL_DIR)")
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
        tokens=str(tokens), num_threads=threads, model_type="nemo_transducer",
        decoding_method="greedy_search")


def split_points(audio: np.ndarray, max_seconds: float) -> list[tuple[int, int]]:
    """Sample ranges covering ``audio``, each ≤ ``max_seconds``, cut in the quietest
    200 ms between half and all of ``max_seconds`` from the previous cut."""
    n = audio.size
    max_len = int(max_seconds * _SAMPLE_RATE)
    if n <= max_len:
        return [(0, n)]
    frame = _SAMPLE_RATE // 50  # 20 ms
    energy = np.convolve(
        np.square(audio[: n // frame * frame].reshape(-1, frame)).mean(axis=1),
        np.ones(10) / 10, mode="same")  # smoothed over 200 ms
    out, start = [], 0
    while n - start > max_len:
        lo, hi = (start + max_len // 2) // frame, (start + max_len) // frame
        cut = (lo + int(np.argmin(energy[lo:hi]))) * frame + frame // 2
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


def tokens_to_pieces(result: Any, offset: float, gap_seconds: float) -> list[TranscriptPiece]:
    """Group a sherpa-onnx result's tokens into sentence pieces, times + ``offset``.

    A token that starts a word begins with a space. A piece ends after a word that ends
    with sentence punctuation, or before a word that starts > ``gap_seconds`` after the
    previous token ended.
    """
    timed = _token_times(result)
    if not timed or not timed[0]:
        text = (result.text or "").strip()
        return [TranscriptPiece(offset, offset, text)] if text else []
    tokens, starts, durs = timed
    pieces: list[TranscriptPiece] = []
    text, p_start, p_end = "", None, 0.0
    for i, tok in enumerate(tokens):
        new_word = tok.startswith(" ") or not text
        if text and new_word and (text.rstrip().endswith(_SENTENCE_END)
                                  or starts[i] - p_end > gap_seconds):
            pieces.append(TranscriptPiece(offset + p_start, offset + p_end, text.strip()))
            text, p_start = "", None
        if p_start is None:
            p_start = starts[i]
        text += tok
        p_end = max(p_end, starts[i] + max(durs[i], 0.0))
    if text.strip():
        pieces.append(TranscriptPiece(offset + p_start, offset + p_end, text.strip()))
    return pieces


def _token_times(result: Any) -> tuple[list[str], list[float], list[float]] | None:
    """(tokens, starts, durations) of a sherpa-onnx result; None without timestamps."""
    tokens = list(result.tokens)
    starts = [float(s) for s in result.timestamps]
    if len(starts) != len(tokens):
        return None
    durs = [float(d) for d in (getattr(result, "durations", None) or [])]
    if len(durs) != len(tokens):  # older sherpa-onnx: estimate from the next token
        durs = [min(0.08, (starts[i + 1] - starts[i]) if i + 1 < len(tokens) else 0.08)
                for i in range(len(tokens))]
    return tokens, starts, durs
