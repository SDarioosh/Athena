from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server import audio
from convo_server.vad import (
    EnergyVAD,
    GainNormalizedVAD,
    Span,
    level_dbfs,
    make_vad,
    merge_spans,
)


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


class _Recorder:
    """Fake inner VAD: remembers the audio it was given."""

    def __init__(self) -> None:
        self.seen: np.ndarray | None = None

    def detect(self, audio: np.ndarray) -> list[Span]:
        self.seen = audio
        return [Span(0.0, 1.0)]


def test_level_dbfs_measures_loud_part():
    x = np.concatenate([silence(9), tone(200, 1)])  # 10% tone, RMS 0.3 / sqrt(2)
    assert abs(level_dbfs(x) - 20 * np.log10(0.3 / np.sqrt(2))) < 1.0
    assert level_dbfs(silence(1)) <= -100


def test_gain_normalized_vad_boosts_quiet_audio_only_for_vad():
    quiet = tone(200, 2) * 0.01  # ~-53 dBFS
    inner = _Recorder()
    vad = GainNormalizedVAD(inner, target_dbfs=-20.0, max_gain_db=30.0)
    assert vad.detect(quiet) == [Span(0.0, 1.0)]
    assert inner.seen is not None
    assert abs(level_dbfs(inner.seen) - (level_dbfs(quiet) + 30.0)) < 0.1  # capped gain
    assert np.max(np.abs(quiet)) < 0.01  # the caller's (stored) audio is untouched


def test_gain_normalized_vad_never_turns_down_loud_audio():
    loud = tone(200, 1)
    inner = _Recorder()
    GainNormalizedVAD(inner, target_dbfs=-20.0).detect(loud)
    assert inner.seen is loud


def test_gain_normalization_lets_energy_vad_find_quiet_speech():
    quiet = np.concatenate([silence(1), tone(200, 2) * 0.03, silence(1)])
    assert EnergyVAD().detect(quiet) == []
    spans = GainNormalizedVAD(EnergyVAD()).detect(quiet)
    assert len(spans) == 1 and abs(spans[0].start - 1) < 0.05


def test_gain_normalization_keeps_digital_silence_silent():
    assert GainNormalizedVAD(EnergyVAD()).detect(silence(3)) == []


def test_make_vad_uses_settings(tmp_path):
    from convo_server.config import Settings

    s = Settings(ingest_token="x" * 16, data_dir=tmp_path, vad_max_gain_db=0)
    assert isinstance(make_vad("energy", s), EnergyVAD)
    with pytest.raises(ValueError):
        make_vad("nope", s)


def test_make_vad_wires_silero_settings(tmp_path, monkeypatch):
    from convo_server import vad as vad_mod
    from convo_server.config import Settings

    class FakeSilero:
        def __init__(self, **kw) -> None:
            self.kw = kw

        def detect(self, audio: np.ndarray) -> list[Span]:
            return []

    monkeypatch.setattr(vad_mod, "SileroVAD", FakeSilero)
    s = Settings(ingest_token="x" * 16, data_dir=tmp_path, vad_threshold=0.35,
                 vad_min_silence_ms=800, vad_speech_pad_ms=100, vad_target_dbfs=-18,
                 vad_max_gain_db=24)
    v = make_vad("silero", s)
    assert isinstance(v, GainNormalizedVAD)
    assert (v.target_dbfs, v.max_gain_db) == (-18, 24)
    assert v.inner.kw == {"threshold": 0.35, "min_silence_ms": 800, "speech_pad_ms": 100}
    off = make_vad("silero", Settings(ingest_token="x" * 16, data_dir=tmp_path,
                                      vad_max_gain_db=0))
    assert isinstance(off, FakeSilero)
