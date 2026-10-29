"""Audio decoding helpers. Everything is normalised to 16 kHz mono float32."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

import numpy as np

from .opus_packets import ogg_opus, parse_length_prefixed

SAMPLE_RATE = 16_000

# Codecs a device may declare. Anything except raw PCM goes through ffmpeg, which
# probes the container (Ogg/Opus from the Omi relay, wav, ...). The Zepp Recorder's
# container-less .opus is wrapped in Ogg first (opus_packets.py).
KNOWN_CODECS = {"opus", "ogg", "wav", "pcm16", "mp3", "m4a", "aac", "webm"}


class AudioDecodeError(RuntimeError):
    pass


def pcm16_to_float(data: bytes) -> np.ndarray:
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def decode(data: bytes, codec: str = "opus", sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode an uploaded chunk to 16 kHz mono float32.

    ``codec="pcm16"`` means raw little-endian 16-bit mono at ``sample_rate``.
    """
    codec = codec.lower()
    if codec == "pcm16":
        audio = pcm16_to_float(data)
        if sample_rate != SAMPLE_RATE:
            audio = resample(audio, sample_rate, SAMPLE_RATE)
        return audio
    if codec == "wav":
        try:
            return _decode_wav(data)
        except (wave.Error, EOFError, AudioDecodeError):
            pass  # unusual WAV flavour, let ffmpeg try
    if codec == "opus" and not data.startswith(b"OggS"):
        # Zepp OS Recorder: raw length-prefixed Opus packets, no container.
        packets = parse_length_prefixed(data)
        if packets:
            data = ogg_opus(packets)
    return _decode_ffmpeg(data)


def _decode_wav(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise AudioDecodeError("only 16-bit WAV handled natively")
        frames = w.readframes(w.getnframes())
        channels, rate = w.getnchannels(), w.getframerate()
    audio = pcm16_to_float(frames)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        audio = resample(audio, rate, SAMPLE_RATE)
    return audio.astype(np.float32)


def _decode_ffmpeg(data: bytes) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioDecodeError("ffmpeg not found on PATH")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AudioDecodeError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    return pcm16_to_float(proc.stdout)


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample; adequate for speech going into VAD/ASR."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(float_to_pcm16(audio))
    return buf.getvalue()


def duration_seconds(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    return audio.size / sample_rate


def bounded_gain(audio: np.ndarray, target: float = 0.8, max_gain: float = 4.0,
                 deadband: float | None = None) -> tuple[np.ndarray, float]:
    """Scale ``audio`` toward a peak of ``target`` (full scale = 1.0); return it and the gain.

    Omi's bounded peak AGC (docs/omi-audio-quality.md §2.2): one uniform gain for the
    whole buffer, capped at ``max_gain``, never below 1 (no attenuation), so the gained
    peak never exceeds ``target`` and the buffer can't clip. Digital silence is returned
    unchanged. With ``deadband``, a buffer whose peak is above it is returned unchanged
    (gaining audio that is already loud raised ASR errors in Omi's tests).
    The input is never modified; with a gain of 1 the same array comes back.
    """
    if not 0.0 < target <= 1.0:
        raise ValueError("target must be in (0, 1]")
    if audio.size == 0 or max_gain <= 1.0:
        return audio, 1.0
    peak = float(np.max(np.abs(audio)))
    if peak <= 0.0 or peak >= target or (deadband is not None and peak > deadband):
        return audio, 1.0
    gain = min(max_gain, target / peak)
    return (audio * gain).astype(np.float32), gain
