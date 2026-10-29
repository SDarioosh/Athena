"""Zepp Recorder .opus: raw length-prefixed Opus packets, wrapped in Ogg for ffmpeg."""

from __future__ import annotations

import shutil
import struct
import subprocess

import pytest

from athena import audio
from athena.opus_packets import ogg_opus, packet_samples, parse_length_prefixed
from tests.conftest import dominant_freq, tone, wav

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def ogg_packets(ogg: bytes) -> list[bytes]:
    """Minimal Ogg reader: the packets of a single-stream file."""
    packets, current, pos = [], b"", 0
    while pos < len(ogg):
        assert ogg[pos:pos + 4] == b"OggS"
        n = ogg[pos + 26]
        lacing = ogg[pos + 27:pos + 27 + n]
        pos += 27 + n
        for size in lacing:
            current += ogg[pos:pos + size]
            pos += size
            if size < 255:
                packets.append(current)
                current = b""
    return packets


def zepp_file(packets: list[bytes]) -> bytes:
    """The Recorder's layout: [u32 BE length][u32 BE final range][packet]."""
    return b"".join(struct.pack(">II", len(p), 0x024A3CDC) + p for p in packets)


def encode_opus(pcm_wav: bytes) -> list[bytes]:
    """Encode like the watch: SILK wideband, mono, 60 ms packets."""
    ogg = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-c:a", "libopus", "-application", "voip", "-ar", "16000", "-ac", "1",
         "-b:a", "16k", "-frame_duration", "60", "-f", "ogg", "pipe:1"],
        input=pcm_wav, capture_output=True, check=True).stdout
    return ogg_packets(ogg)[2:]  # drop OpusHead and OpusTags


def test_parse_matches_recorder_header_seen_on_the_watch():
    # First packet of a real T-Rex 3 Pro chunk: length 0x45, final range, TOC 0x58
    # (config 11: SILK wideband, 60 ms, mono). Payload bytes replaced with filler.
    first = bytes([0x58]) + bytes(68)
    data = bytes.fromhex("00000045024a3cdc") + first
    assert parse_length_prefixed(data) == [first]
    assert packet_samples(first) == 2880  # 60 ms at 48 kHz


def test_parse_rejects_other_data():
    assert parse_length_prefixed(b"") is None
    assert parse_length_prefixed(b"OggS" + bytes(60)) is None
    assert parse_length_prefixed(b"\x00\x00\x00\x05\x00\x00\x00\x00\x58") is None  # short
    assert parse_length_prefixed(bytes.fromhex("0000ffff00000000") + bytes(64)) is None
    mixed = zepp_file([bytes([0x58]) + bytes(10), bytes([0x5C]) + bytes(10)])
    assert parse_length_prefixed(mixed) is None  # mono and stereo mixed
    # Bandwidth may change mid-recording (WB 60 ms, then NB 60 ms).
    switched = [bytes([0x58]) + bytes(10), bytes([0x18]) + bytes(10)]
    assert parse_length_prefixed(zepp_file(switched)) == switched


def test_packet_samples_frame_counts():
    assert packet_samples(bytes([0x58])) == 2880     # config 11, SILK WB 60 ms
    assert packet_samples(bytes([0x48])) == 960      # config 9, SILK WB 20 ms
    assert packet_samples(bytes([0x49])) == 1920     # 2 frames
    assert packet_samples(bytes([0x4B, 0x03])) == 2880  # code 3, 3 frames
    assert packet_samples(bytes([0x68])) == 960      # config 13, hybrid 20 ms
    assert packet_samples(bytes([0xF8])) == 960      # config 31, CELT FB 20 ms
    assert packet_samples(bytes([0x80])) == 120      # config 16, CELT 2.5 ms


def test_recording_cut_off_mid_write_keeps_whole_packets():
    good = [bytes([0x58]) + bytes(20), bytes([0x58]) + bytes(30)]
    data = zepp_file(good)
    assert parse_length_prefixed(data + bytes.fromhex("00000040")) == good  # half a header
    assert parse_length_prefixed(data + zepp_file([bytes([0x58]) + bytes(40)])[:-5]) == good


@needs_ffmpeg
def test_recorder_opus_decodes_to_the_original_audio():
    packets = encode_opus(wav(tone(400, 3.0)))
    assert packets[0][0] >> 3 < 12  # SILK, like the watch
    assert packet_samples(packets[0]) == 2880
    data = zepp_file(packets)
    assert not data.startswith(b"OggS")
    out = audio.decode(data, "opus")
    assert abs(out.size / audio.SAMPLE_RATE - 3.0) < 0.1
    assert abs(dominant_freq(out) - 400) < 15


@needs_ffmpeg
def test_ogg_opus_input_is_left_alone():
    ogg = ogg_opus(encode_opus(wav(tone(300, 1.0))))
    assert ogg.startswith(b"OggS")
    out = audio.decode(ogg, "opus")
    assert abs(dominant_freq(out) - 300) < 15
