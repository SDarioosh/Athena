"""whisper.cpp transcription backends.

Two ways to run whisper.cpp locally:

* ``WhisperServerTranscriber`` talks to a running ``whisper-server`` (model stays
  loaded, preferred).
* ``WhisperCliTranscriber`` shells out to ``whisper-cli`` per request.
"""

from __future__ import annotations

import json
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
    suppress_non_speech: bool = False
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
                 temperature_inc: float = 0.2, decode: DecodeOptions | None = None) -> None:
        self.binary = binary
        self.model = model
        self.language = language
        self.threads = threads
        self.decode = decode or DecodeOptions(temperature_inc=temperature_inc)

    def command(self, wav: Path, out_base: Path, prompt: str = "") -> list[str]:
        cmd = [self.binary, "-m", self.model, "-f", str(wav), "-l", self.language,
               "-t", str(self.threads), *self.decode.cli_args(),
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
