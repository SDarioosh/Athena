"""wer.py plumbing: ffmpeg versions, WER math, whisper-server request (fake transcriber)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SPIKE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SPIKE))

import wer  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
REF = "Fictional Alice asked whether the blue prototype ships on Thursday."


def _tone(path: Path, seconds: float = 2.0, rate: int = 32000, fmt: str = "wav") -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"sine=frequency=440:sample_rate={rate}:duration={seconds}", "-ac", "1"]
    args += ["-f", "s16le"] if fmt == "pcm" else []
    subprocess.run([*args, str(path)], check=True)


def test_normalize_and_builtin_wer():
    assert wer.normalize("Hello, World!  It's  ‘fine’.") == "hello world it's fine"
    assert wer._edit_distance(["a", "b", "c", "d"], ["a", "x", "c"]) == 2
    errors, n, _ = wer.word_errors(REF, REF.upper())
    assert (errors, n) == (0, 10)


def test_builtin_matches_jiwer_when_installed(monkeypatch):
    hyp = "fictional alice asked if the blue prototype ships thursday today"
    got = wer.word_errors(REF, hyp)
    monkeypatch.setitem(sys.modules, "jiwer", None)  # force the built-in path
    assert wer.word_errors(REF, hyp)[:2] == got[:2]


def test_empty_reference_is_an_error():
    with pytest.raises(ValueError):
        wer.word_errors(" ... ", "anything")


@needs_ffmpeg
def test_versions_are_16k_mono_and_opus_is_32k(tmp_path):
    src = tmp_path / "alice.wav"
    _tone(src, seconds=4.0)
    (tmp_path / "work").mkdir()
    v = wer.make_versions(src, tmp_path / "work", 32000)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_name,sample_rate,channels", "-of", "json",
                            str(v["opus"])], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "opus" and stream["channels"] == 1
    for key in ("wav", "opus_wav"):
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                               "stream=sample_rate,channels", "-of", "json", str(v[key])],
                              capture_output=True, text=True, check=True)
        s = json.loads(info.stdout)["streams"][0]
        assert (s["sample_rate"], s["channels"]) == ("16000", 1)
    kbps = v["opus"].stat().st_size * 8 / 1000 / 4.0
    assert 20 < kbps < 45


@needs_ffmpeg
def test_evaluate_with_fake_transcriber_and_bot_pcm(tmp_path):
    bot_out = tmp_path / "out"
    bot_out.mkdir()
    _tone(bot_out / "track-16778240.pcm", rate=48000, fmt="pcm")
    (bot_out / "participants.json").write_text(json.dumps({"sample_rate": 48000}))
    (bot_out / "track-16778240.txt").write_text(REF)
    seen: list[str] = []

    def fake(wav: Path) -> str:
        seen.append(wav.name)
        # pretend Opus costs one substituted word
        return REF.replace("blue", "glue") if wav.name.endswith(".opus.wav") else REF

    pairs = wer.collect([bot_out], [])
    assert [a.name for a, _ in pairs] == ["track-16778240.pcm"]
    assert wer.pcm_rate_for(pairs[0][0], 32000) == 48000
    rows, _engine = wer.evaluate(pairs, fake, keep=tmp_path / "keep")
    assert seen == ["track-16778240.wav", "track-16778240.opus.wav"]
    (row,) = rows
    assert (row.words, row.wav_errors, row.opus_errors) == (10, 0, 1)
    md = wer.table(rows, "test", "http://fake")
    assert "| track-16778240.pcm | 10 | 0.0 | 10.0 | +10.0 |" in md
    assert "| **all** | 10 | 0.0 | 10.0 | +10.0 | |" in md
    # input lasted 2 s at 48 kHz; the 16 kHz WAV must too
    assert abs(wer.duration_seconds(tmp_path / "keep" / "track-16778240.wav") - 2.0) < 0.05


def test_whisper_server_request_shape(tmp_path):
    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got["path"] = self.path
            got["ctype"] = self.headers["Content-Type"]
            got["body"] = body
            out = json.dumps({"text": " Fictional words. "}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"RIFFfake")
        text = wer.WhisperServer(f"http://127.0.0.1:{srv.server_port}/")(wav)
    finally:
        srv.shutdown()
    assert text == "Fictional words."
    assert got["path"] == "/inference"
    assert got["ctype"].startswith("multipart/form-data; boundary=")
    for part in (b'name="response_format"\r\n\r\njson', b'name="temperature"\r\n\r\n0.0',
                 b'name="language"\r\n\r\nen', b'filename="x.wav"', b"RIFFfake"):
        assert part in got["body"]
