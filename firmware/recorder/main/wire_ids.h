/* Values on the wire that the phone and PC relays, and chunks already queued on a card,
 * rely on (docs/twatch-ble-protocol.md). The macro names follow the project's name; the
 * bytes never change. test/test_wire_ids.c pins them byte for byte. */
#pragma once

/* f806xxxx-61c0-4fda-8b7b-26b08e916438 (§3), little-endian as NimBLE stores it: the 16
 * bytes of the service (n = 0) and characteristic (n = 1..4) UUIDs. */
#define ATHENA_UUID_BYTES(n)                                                                \
    0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, (n) & 0xff, \
        ((n) >> 8) & 0xff, 0x06, 0xf8

/* Opus comment tags each chunk carries (§2.1), each followed by its value. */
#define ATHENA_TAG_SESSION "BARVIS_SESSION="
#define ATHENA_TAG_SEQUENCE "BARVIS_SEQUENCE="
#define ATHENA_TAG_START_MS "BARVIS_START_MS="
