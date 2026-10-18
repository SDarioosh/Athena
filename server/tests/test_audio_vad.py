from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server import audio
from convo_server.vad import EnergyVAD, Span, merge_spans


def test_pcm16_roundtrip():
    x = tone(440, 0.5)
    y = audio.pcm16_to_float(audio.float_to_pcm16(x))
    assert np.max(np.abs(x - y)) < 1e-3


def test_decode_pcm16_odd_length_and_resample():
    raw = audio.float_to_pcm16(tone(300, 1.0)) + b"\x00"
    assert audio.decode(raw, "pcm16").size == audio.SAMPLE_RATE
    assert audio.decode(audio.float_to_pcm16(tone(300, 1.0)), "pcm16", 8000).size == 32000


def test_decode_wav():
    out = audio.decode(wav(tone(300, 1.5)), "wav")
    assert abs(audio.duration_seconds(out) - 1.5) < 0.01


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_decode_opus_via_ffmpeg():
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-c:a", "libopus",
         "-b:a", "24k", "-f", "ogg", "pipe:1"],
        input=wav(tone(300, 2.0)), capture_output=True, check=True)
    out = audio.decode(proc.stdout, "opus")
    assert abs(audio.duration_seconds(out) - 2.0) < 0.1


def test_decode_garbage_raises():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with pytest.raises(audio.AudioDecodeError):
        audio.decode(b"definitely not audio", "opus")


def test_energy_vad_finds_bursts():
    spans = EnergyVAD().detect(np.concatenate([silence(1), tone(200, 2), silence(3),
                                               tone(200, 1)]))
    assert len(spans) == 2
    assert abs(spans[0].start - 1) < 0.05 and abs(spans[0].end - 3) < 0.05
    assert abs(spans[1].start - 6) < 0.05


def test_merge_spans():
    merged = merge_spans([Span(3, 4), Span(0, 1), Span(1.2, 2)], 0.5)
    assert merged == [Span(0, 2), Span(3, 4)]
