"""Raw Opus packets to Ogg Opus, so ffmpeg can decode them.

The Zepp OS ``@zos/media`` Recorder (``codec.OPUS``) writes no container. Each packet is
``[u32 BE length][u32 BE encoder final range][packet]``, the layout of libopus's
``opus_demo`` tool. ffmpeg can't probe that, so we wrap the packets in Ogg first.
"""

from __future__ import annotations

import struct

MAX_PACKET = 1275  # largest Opus packet (RFC 6716 §3.4)
GRANULE_RATE = 48_000  # Ogg Opus granule positions always count 48 kHz samples


def parse_length_prefixed(data: bytes) -> list[bytes] | None:
    """Split ``opus_demo``-style data into packets, or None if it isn't that layout.

    The layout has no magic number, so the packets must cover the data (an incomplete
    last packet, from a recording cut off mid-write, is dropped), every length must
    be a valid Opus packet size, and every packet must have the same channel count (the
    encoder may switch mode or bandwidth mid-recording, but not channels).
    """
    packets: list[bytes] = []
    pos, stereo = 0, None
    while pos < len(data):
        if pos + 8 > len(data):
            break  # cut off inside a header
        (length,) = struct.unpack_from(">I", data, pos)
        start = pos + 8
        if not 1 <= length <= MAX_PACKET:
            return None
        if start + length > len(data):
            break  # cut off inside a packet
        packet = data[start:start + length]
        if stereo is None:
            stereo = packet[0] & 0x04
        elif packet[0] & 0x04 != stereo:
            return None
        packets.append(packet)
        pos = start + length
    return packets or None


def packet_samples(packet: bytes) -> int:
    """Samples at 48 kHz in one Opus packet (RFC 6716 §3.1)."""
    config = packet[0] >> 3
    if config < 12:  # SILK: 10, 20, 40, 60 ms
        frame = (480, 960, 1920, 2880)[config % 4]
    elif config < 16:  # hybrid: 10, 20 ms
        frame = (480, 960)[config % 2]
    else:  # CELT: 2.5, 5, 10, 20 ms
        frame = (120, 240, 480, 960)[config % 4]
    code = packet[0] & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        frames = packet[1] & 0x3F if len(packet) > 1 else 0
    return frame * frames


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def _ogg_crc(page: bytes) -> int:
    crc = 0
    for b in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


def _page(payload: bytes, *, granule: int, serial: int, seq: int, flags: int) -> bytes:
    lacing = bytes([255] * (len(payload) // 255) + [len(payload) % 255])
    header = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, seq, 0,
                         len(lacing))
    page = bytearray(header + lacing + payload)
    struct.pack_into("<I", page, 22, _ogg_crc(bytes(page)))
    return bytes(page)


def ogg_opus(packets: list[bytes], *, input_rate: int = 16_000, serial: int = 0x5A455050
             ) -> bytes:
    """Wrap Opus packets in an Ogg Opus stream (RFC 7845), one packet per page."""
    channels = 2 if packets and packets[0][0] & 0x04 else 1
    head = struct.pack("<8sBBHIhB", b"OpusHead", 1, channels, 0, input_rate, 0, 0)
    vendor = b"barvis-sound"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    out = [_page(head, granule=0, serial=serial, seq=0, flags=0x02),
           _page(tags, granule=0, serial=serial, seq=1, flags=0)]
    granule = 0
    for i, packet in enumerate(packets):
        granule += packet_samples(packet)
        last = i == len(packets) - 1
        out.append(_page(packet, granule=granule, serial=serial, seq=i + 2,
                         flags=0x04 if last else 0))
    return b"".join(out)
