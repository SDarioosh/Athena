"""whisper.cpp transcription backends.

Two ways to run whisper.cpp locally:

* ``WhisperServerTranscriber`` talks to a running ``whisper-server`` (model stays
  loaded, preferred).
* ``WhisperCliTranscriber`` shells out to ``whisper-cli`` per request.
"""

from __future__ import annotations

import json
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
    def transcribe(self, audio: np.ndarray) -> list[TranscriptPiece]: ...


def _clean(pieces: list[TranscriptPiece]) -> list[TranscriptPiece]:
    out = []
    for p in pieces:
        text = p.text.strip()
        # whisper.cpp emits these for non-speech; never store them as words
        if not text or text in {"[BLANK_AUDIO]", "[MUSIC]", "(silence)", "[ Silence ]"}:
            continue
        out.append(TranscriptPiece(p.start, max(p.end, p.start), text))
    return out


class WhisperServerTranscriber:
    def __init__(self, base_url: str, language: str = "en", timeout: float = 600.0) -> None:
        self.url = base_url.rstrip("/") + "/inference"
        self.language = language
        self.timeout = timeout

    def transcribe(self, audio: np.ndarray) -> list[TranscriptPiece]:
        resp = httpx.post(
            self.url,
            files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
            data={"response_format": "verbose_json", "language": self.language,
                  "temperature": "0.0"},
            timeout=self.timeout,
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
    def __init__(self, binary: str, model: str, language: str = "en", threads: int = 4) -> None:
        self.binary = binary
        self.model = model
        self.language = language
        self.threads = threads

    def transcribe(self, audio: np.ndarray) -> list[TranscriptPiece]:
        with tempfile.TemporaryDirectory(prefix="convo-whisper-") as tmp:
            wav = Path(tmp) / "audio.wav"
            wav.write_bytes(to_wav_bytes(audio))
            out_base = Path(tmp) / "out"
            subprocess.run(
                [self.binary, "-m", self.model, "-f", str(wav), "-l", self.language,
                 "-t", str(self.threads), "-oj", "-of", str(out_base), "-np"],
                check=True, capture_output=True,
            )
            return parse_cli_json(json.loads(out_base.with_suffix(".json").read_text()))


def parse_cli_json(payload: dict) -> list[TranscriptPiece]:
    return _clean([
        TranscriptPiece(s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0, s["text"])
        for s in payload.get("transcription", [])
    ])
