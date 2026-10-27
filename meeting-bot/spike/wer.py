#!/usr/bin/env python3
"""Opus 32 kbps vs WAV: word error rate of whisper on the same tracks (Phase 0 spike).

For each fixture track (an audio file plus a reference text), it makes two versions with
ffmpeg, the way Athena would get them from the bot:

- WAV: 16 kHz mono PCM, the baseline;
- Opus: `-c:a libopus -b:a 32k -ar 16000 -ac 1` (Ogg), decoded back to 16 kHz WAV with
  ffmpeg, as Athena's ingest does, before it goes to whisper.

Both go to whisper-server's `/inference` (the server's model; URL from --whisper-url or
`CONVO_WHISPER_SERVER_URL`). WER uses jiwer when installed (server `eval` extra), else a
built-in word-level edit distance; both normalize case and punctuation the same way.

    python3 wer.py --whisper-url http://127.0.0.1:8178 --dir fixtures/
    python3 wer.py --track out/track-16778240.pcm alice.txt --pcm-rate 32000

`--dir` takes every audio file with a same-name `.txt` reference next to it. A `.pcm`
file is raw s16le mono from the spike bot; its rate comes from `participants.json` in
the same folder, else from --pcm-rate. Prints a Markdown table (and writes it to --out).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXT = {".wav", ".pcm", ".flac", ".ogg", ".opus", ".mp3", ".m4a", ".webm"}
OPUS_ARGS = ["-c:a", "libopus", "-b:a", "32k", "-ar", "16000", "-ac", "1"]
WAV_ARGS = ["-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1"]

Transcriber = Callable[[Path], str]


# --- audio --------------------------------------------------------------------------------

def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def input_args(audio: Path, pcm_rate: int) -> list[str]:
    if audio.suffix == ".pcm":
        return ["-f", "s16le", "-ar", str(pcm_rate), "-ac", "1", "-i", str(audio)]
    return ["-i", str(audio)]


def make_versions(audio: Path, work: Path, pcm_rate: int) -> dict[str, Path]:
    """Return {"wav": 16 kHz WAV, "opus": the Ogg/Opus file, "opus_wav": Opus decoded}."""
    stem = work / audio.stem
    wav, opus, opus_wav = (stem.with_suffix(".wav"), stem.with_suffix(".ogg"),
                           Path(f"{stem}.opus.wav"))
    src = input_args(audio, pcm_rate)
    _ffmpeg([*src, *WAV_ARGS, str(wav)])
    _ffmpeg([*src, *OPUS_ARGS, str(opus)])
    _ffmpeg(["-i", str(opus), *WAV_ARGS, str(opus_wav)])
    return {"wav": wav, "opus": opus, "opus_wav": opus_wav}


def pcm_rate_for(audio: Path, default: int) -> int:
    side = audio.parent / "participants.json"
    if audio.suffix == ".pcm" and side.is_file():
        try:
            rate = int(json.loads(side.read_text()).get("sample_rate") or 0)
        except (ValueError, OSError):
            rate = 0
        if rate > 0:
            return rate
    return default


# --- whisper-server ---------------------------------------------------------------------

class WhisperServer:
    """POST a WAV to whisper.cpp's whisper-server /inference and return the text."""

    def __init__(self, base_url: str, language: str = "en", timeout: float = 600.0) -> None:
        self.url = base_url.rstrip("/") + "/inference"
        self.language = language
        self.timeout = timeout

    def __call__(self, wav: Path) -> str:
        boundary = uuid.uuid4().hex
        fields = {"response_format": "json", "temperature": "0.0", "language": self.language}
        body = bytearray()
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{wav.name}\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
        body += wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.url, data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (local URL)
            payload = json.loads(resp.read())
        return str(payload.get("text", "")).strip()


# --- WER ----------------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(w.strip("'") for w in text.split() if w.strip("'"))


def _edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def word_errors(reference: str, hypothesis: str) -> tuple[int, int, str]:
    """Return (errors, reference words, engine) after normalizing both texts."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    n = len(ref.split())
    if n == 0:
        raise ValueError("empty reference text")
    try:
        import jiwer
    except ImportError:
        return _edit_distance(ref.split(), hyp.split()), n, "built-in"
    out = jiwer.process_words(ref, hyp if hyp else "")
    return out.substitutions + out.deletions + out.insertions, n, "jiwer"


# --- run ----------------------------------------------------------------------------------

@dataclass
class Row:
    track: str
    words: int
    wav_errors: int
    opus_errors: int
    opus_kbps: float

    @property
    def wav_wer(self) -> float:
        return 100.0 * self.wav_errors / self.words

    @property
    def opus_wer(self) -> float:
        return 100.0 * self.opus_errors / self.words


def duration_seconds(wav: Path) -> float:
    # 16-bit mono 16 kHz WAV with a 44-byte header (ffmpeg's pcm_s16le output)
    return max(wav.stat().st_size - 44, 0) / (2 * 16000)


def evaluate(pairs: list[tuple[Path, Path]], transcribe: Transcriber, pcm_rate: int = 32000,
             keep: Path | None = None) -> tuple[list[Row], str]:
    rows: list[Row] = []
    engine = "built-in"
    with tempfile.TemporaryDirectory(prefix="athena-wer-") as tmp:
        work = keep or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        for audio, ref_file in pairs:
            ref = ref_file.read_text(encoding="utf-8")
            v = make_versions(audio, work, pcm_rate_for(audio, pcm_rate))
            wav_err, n, engine = word_errors(ref, transcribe(v["wav"]))
            opus_err, _, _ = word_errors(ref, transcribe(v["opus_wav"]))
            secs = duration_seconds(v["wav"]) or 1.0
            rows.append(Row(audio.name, n, wav_err, opus_err,
                            v["opus"].stat().st_size * 8 / 1000 / secs))
    return rows, engine


def table(rows: list[Row], engine: str, url: str) -> str:
    out = [f"WER engine: {engine}; whisper-server: {url}; Opus: {' '.join(OPUS_ARGS)}", "",
           "| Track | Words | WAV WER % | Opus 32k WER % | Δ (Opus − WAV) pp | Opus kbps |",
           "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        out.append(f"| {r.track} | {r.words} | {r.wav_wer:.1f} | {r.opus_wer:.1f} | "
                   f"{r.opus_wer - r.wav_wer:+.1f} | {r.opus_kbps:.1f} |")
    words = sum(r.words for r in rows)
    if words:
        wav = 100.0 * sum(r.wav_errors for r in rows) / words
        opus = 100.0 * sum(r.opus_errors for r in rows) / words
        out.append(f"| **all** | {words} | {wav:.1f} | {opus:.1f} | {opus - wav:+.1f} | |")
    return "\n".join(out) + "\n"


def collect(dirs: list[Path], tracks: list[list[str]]) -> list[tuple[Path, Path]]:
    pairs = [(Path(a), Path(r)) for a, r in tracks]
    for d in dirs:
        for audio in sorted(d.iterdir()):
            ref = audio.with_suffix(".txt")
            if audio.suffix.lower() in AUDIO_EXT and ref.is_file():
                pairs.append((audio, ref))
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--whisper-url", default=os.environ.get("CONVO_WHISPER_SERVER_URL",
                                                            "http://127.0.0.1:8178"))
    ap.add_argument("--language", default="en")
    ap.add_argument("--dir", type=Path, action="append", default=[],
                    help="folder of audio files with same-name .txt references")
    ap.add_argument("--track", nargs=2, action="append", default=[], metavar=("AUDIO", "REF"))
    ap.add_argument("--pcm-rate", type=int, default=32000,
                    help="rate of .pcm inputs without participants.json (default 32000)")
    ap.add_argument("--keep", type=Path, help="keep the WAV and Opus versions here")
    ap.add_argument("--out", type=Path, help="also write the table to this file")
    args = ap.parse_args(argv)
    pairs = collect(args.dir, args.track)
    if not pairs:
        ap.error("no tracks: give --dir or --track AUDIO REF")
    rows, engine = evaluate(pairs, WhisperServer(args.whisper_url, args.language),
                            args.pcm_rate, args.keep)
    text = table(rows, engine, args.whisper_url)
    print(text, end="")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
