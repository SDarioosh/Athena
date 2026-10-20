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


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        """``prompt``: vocabulary and names that help whisper spell them right."""
        ...


# whisper.cpp marks non-speech as a bracketed or starred tag: [BLANK_AUDIO], [MUSIC],
# (silence), (upbeat music), *laughs*. A piece that is only such tags is not words.
_NON_SPEECH = re.compile(r"^(\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*)+$")


def _clean(pieces: list[TranscriptPiece]) -> list[TranscriptPiece]:
    out = []
    for p in pieces:
        text = p.text.strip()
        if not text or _NON_SPEECH.match(text):
            continue
        out.append(TranscriptPiece(p.start, max(p.end, p.start), text))
    return out


class WhisperServerTranscriber:
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
        return parse_verbose_json(resp.json())


def parse_verbose_json(payload: dict) -> list[TranscriptPiece]:
    segs = payload.get("segments")
    if segs is None:  # plain {"text": ...} response
        text = payload.get("text", "")
        return _clean([TranscriptPiece(0.0, 0.0, text)]) if text.strip() else []
    return _clean([TranscriptPiece(float(s["start"]), float(s["end"]), s["text"])
                   for s in segs])


class WhisperCliTranscriber:
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
               "-oj", "-of", str(out_base), "-np"]
        return cmd + (["--prompt", prompt] if prompt else [])

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        with tempfile.TemporaryDirectory(prefix="convo-whisper-") as tmp:
            wav = Path(tmp) / "audio.wav"
            wav.write_bytes(to_wav_bytes(audio))
            out_base = Path(tmp) / "out"
            subprocess.run(self.command(wav, out_base, prompt), check=True,
                           capture_output=True)
            return parse_cli_json(json.loads(out_base.with_suffix(".json").read_text()))


def parse_cli_json(payload: dict) -> list[TranscriptPiece]:
    return _clean([
        TranscriptPiece(s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0, s["text"])
        for s in payload.get("transcription", [])
    ])


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

    ``prompt`` is ignored: Parakeet has no prompt or vocabulary biasing. The language
    is detected by the model (25 European languages), so ``CONVO_LANGUAGE`` is not used.
    """

    def __init__(self, model_dir: str | Path, threads: int = 4, *,
                 max_chunk_seconds: float = 30.0, gap_seconds: float = 0.8,
                 recognizer: Any = None) -> None:
        self.max_chunk_seconds = max_chunk_seconds
        self.gap_seconds = gap_seconds
        self._rec = recognizer if recognizer is not None else _load_parakeet(
            Path(model_dir), threads)

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        audio = np.asarray(audio, dtype=np.float32)
        pieces: list[TranscriptPiece] = []
        for a, b in split_points(audio, self.max_chunk_seconds):
            clip = audio[a:b]
            if clip.size < _SAMPLE_RATE // 10:  # < 100 ms: nothing to recognize
                continue
            stream = self._rec.create_stream()
            stream.accept_waveform(_SAMPLE_RATE, clip)
            self._rec.decode_stream(stream)
            pieces += tokens_to_pieces(stream.result, a / _SAMPLE_RATE, self.gap_seconds)
        return _clean(pieces)


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
    tokens = list(result.tokens)
    starts = list(result.timestamps)
    if not tokens or len(starts) != len(tokens):
        text = (result.text or "").strip()
        return [TranscriptPiece(offset, offset, text)] if text else []
    durs = list(getattr(result, "durations", None) or [])
    if len(durs) != len(tokens):  # older sherpa-onnx: estimate from the next token
        durs = [min(0.08, (starts[i + 1] - starts[i]) if i + 1 < len(tokens) else 0.08)
                for i in range(len(tokens))]
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
