from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server import audio
from convo_server.config import Settings
from convo_server.vad import EnergyVAD, GainedVAD, Span, level_dbfs, make_vad, merge_spans


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


def test_bounded_gain_silence_and_empty_unchanged():
    z = silence(1)
    out, g = audio.bounded_gain(z)
    assert g == 1.0 and out is z
    e = np.zeros(0, dtype=np.float32)
    assert audio.bounded_gain(e)[1] == 1.0


def test_bounded_gain_never_attenuates_loud_audio():
    loud = tone(300, 1, amp=0.95)
    out, g = audio.bounded_gain(loud)
    assert g == 1.0 and out is loud


def test_bounded_gain_reaches_target_and_caps():
    quiet = tone(300, 1, amp=0.4)
    out, g = audio.bounded_gain(quiet, target=0.8, max_gain=4.0)
    assert g == pytest.approx(2.0, rel=1e-3)
    assert np.max(np.abs(out)) == pytest.approx(0.8, rel=1e-3)
    faint = tone(300, 1, amp=0.01)
    out, g = audio.bounded_gain(faint, target=0.8, max_gain=4.0)
    assert g == 4.0
    assert np.max(np.abs(out)) == pytest.approx(0.04, rel=1e-3)
    assert out.dtype == np.float32
    assert np.max(np.abs(faint)) == pytest.approx(0.01, rel=1e-3)  # input untouched


def test_bounded_gain_off_and_no_clipping():
    quiet = tone(300, 1, amp=0.1)
    assert audio.bounded_gain(quiet, max_gain=1.0) == (quiet, 1.0)
    for amp in (0.05, 0.3, 0.7):
        out, _ = audio.bounded_gain(tone(300, 1, amp=amp), target=1.0, max_gain=100.0)
        assert np.max(np.abs(out)) <= 1.0 + 1e-6
    with pytest.raises(ValueError):
        audio.bounded_gain(quiet, target=1.5)


def test_bounded_gain_deadband():
    above = tone(300, 1, amp=0.5)
    out, g = audio.bounded_gain(above, deadband=0.4)
    assert g == 1.0 and out is above
    below = tone(300, 1, amp=0.2)
    out, g = audio.bounded_gain(below, deadband=0.4)
    assert g == pytest.approx(4.0) and np.max(np.abs(out)) == pytest.approx(0.8, rel=1e-3)


class RecordingVAD:
    def __init__(self) -> None:
        self.seen: list[np.ndarray] = []

    def detect(self, audio: np.ndarray) -> list[Span]:
        self.seen.append(audio)
        return [Span(0.0, 1.0)]


def test_gained_vad_scores_a_gained_copy():
    inner = RecordingVAD()
    vad = GainedVAD(inner, max_gain=4.0, target=0.8)
    quiet = tone(300, 1, amp=0.05)
    before = quiet.copy()
    assert vad.detect(quiet) == [Span(0.0, 1.0)]
    assert np.max(np.abs(inner.seen[0])) == pytest.approx(0.2, rel=1e-3)
    assert inner.seen[0] is not quiet
    np.testing.assert_array_equal(quiet, before)  # caller's array untouched


def test_gained_vad_admits_quiet_speech_energy_vad():
    quiet = np.concatenate([silence(1), tone(300, 2, amp=0.015), silence(1)])
    assert EnergyVAD().detect(quiet) == []
    assert GainedVAD(EnergyVAD(), max_gain=4.0).detect(quiet)


def test_make_vad_settings_default_is_off(tmp_path):
    s = Settings(ingest_token="x" * 16, data_dir=tmp_path, _env_file=None)
    assert s.vad_agc_max_gain == 1.0 and s.asr_agc_max_gain == 1.0
    assert s.vad_threshold == 0.5 and s.vad_speech_pad_ms == 30
    assert isinstance(make_vad("energy", s), EnergyVAD)
    assert isinstance(make_vad("energy"), EnergyVAD)
    on = Settings(ingest_token="x" * 16, data_dir=tmp_path, vad_agc_max_gain=8.0,
                  vad_agc_target=0.7)
    vad = make_vad("energy", on)
    assert isinstance(vad, GainedVAD) and vad.max_gain == 8.0 and vad.target == 0.7


def test_level_dbfs_measures_loud_part():
    x = np.concatenate([silence(9), tone(200, 1)])  # 10% tone, RMS 0.3 / sqrt(2)
    assert abs(level_dbfs(x) - 20 * np.log10(0.3 / np.sqrt(2))) < 1.0
    assert level_dbfs(silence(1)) <= -100


def test_make_vad_wires_silero_settings(tmp_path, monkeypatch):
    from convo_server import vad as vad_mod

    class FakeSilero:
        def __init__(self, **kw) -> None:
            self.kw = kw

        def detect(self, audio: np.ndarray) -> list[Span]:
            return []

    monkeypatch.setattr(vad_mod, "SileroVAD", FakeSilero)
    s = Settings(ingest_token="x" * 16, data_dir=tmp_path, _env_file=None,
                 vad_threshold=0.35, vad_min_silence_ms=800, vad_speech_pad_ms=100)
    off = make_vad("silero", s)
    assert isinstance(off, FakeSilero)
    assert off.kw == {"threshold": 0.35, "min_silence_ms": 800, "speech_pad_ms": 100}
    on = make_vad("silero", s.model_copy(update={"vad_agc_max_gain": 4.0}))
    assert isinstance(on, GainedVAD) and isinstance(on.inner, FakeSilero)
    with pytest.raises(ValueError):
        make_vad("nope", s)
