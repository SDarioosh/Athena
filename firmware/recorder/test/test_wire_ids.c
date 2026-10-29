/* Host test for main/wire_ids.h: the BLE service and characteristic UUIDs and the Opus tags
 * keep their exact bytes whatever the macros are called. The expected bytes below are
 * literals, copied from the firmware as it was before the macros were renamed
 * (BARVIS_UUID in ble_sync.c, the tag strings in session_rec.c, OGG_OPUS_VENDOR), and match
 * docs/twatch-ble-protocol.md §2.1 and §3. ble_sync.c needs NimBLE, so the UUIDs are built
 * here the way it builds them: ATHENA_UUID_BYTES(n) inside NimBLE's BLE_UUID128_INIT shape. */
#include "check.h"
#include "ogg_opus.h"
#include "wire_ids.h"

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* The shape of NimBLE's ble_uuid128_t and BLE_UUID128_INIT (host/ble_uuid.h). */
typedef struct {
    uint8_t type;
} fake_ble_uuid_t;
typedef struct {
    fake_ble_uuid_t u;
    uint8_t value[16];
} fake_ble_uuid128_t;
#define FAKE_BLE_UUID128_INIT(uuid128...) {.u = {.type = 128}, .value = {uuid128}}
#define ATHENA_UUID(n) FAKE_BLE_UUID128_INIT(ATHENA_UUID_BYTES(n))

static const fake_ble_uuid128_t s_uuids[] = {
    ATHENA_UUID(0x0000), ATHENA_UUID(0x0001), ATHENA_UUID(0x0002),
    ATHENA_UUID(0x0003), ATHENA_UUID(0x0004),
};

/* f8060000…f8060004-61c0-4fda-8b7b-26b08e916438, little-endian. */
static const uint8_t EXPECTED[5][16] = {
    {0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, 0x00, 0x00, 0x06, 0xf8},
    {0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, 0x01, 0x00, 0x06, 0xf8},
    {0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, 0x02, 0x00, 0x06, 0xf8},
    {0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, 0x03, 0x00, 0x06, 0xf8},
    {0x38, 0x64, 0x91, 0x8e, 0xb0, 0x26, 0x7b, 0x8b, 0xda, 0x4f, 0xc0, 0x61, 0x04, 0x00, 0x06, 0xf8},
};

static void check_str(const char *got, const char *want)
{
    CHECK_EQ(strlen(got), strlen(want));
    CHECK(memcmp(got, want, strlen(want) + 1) == 0);
}

int main(void)
{
    CHECK_EQ(sizeof(s_uuids) / sizeof(s_uuids[0]), 5);
    for (int i = 0; i < 5; i++) {
        CHECK(memcmp(s_uuids[i].value, EXPECTED[i], 16) == 0);
    }
    /* Higher characteristic numbers land in bytes 12 and 13, little-endian. */
    const fake_ble_uuid128_t hi = ATHENA_UUID(0x1234);
    CHECK_EQ(hi.value[12], 0x34);
    CHECK_EQ(hi.value[13], 0x12);
    CHECK_EQ(hi.value[14], 0x06);
    CHECK_EQ(hi.value[15], 0xf8);

    check_str(ATHENA_TAG_SESSION, "BARVIS_SESSION=");
    check_str(ATHENA_TAG_SEQUENCE, "BARVIS_SEQUENCE=");
    check_str(ATHENA_TAG_START_MS, "BARVIS_START_MS=");
    check_str(OGG_OPUS_VENDOR, "barvis-sound recorder");

    /* The comments as session_rec.c formats them. */
    char buf[48];
    snprintf(buf, sizeof(buf), ATHENA_TAG_SEQUENCE "%" PRIu32, (uint32_t)2);
    check_str(buf, "BARVIS_SEQUENCE=2");
    snprintf(buf, sizeof(buf), ATHENA_TAG_START_MS "%" PRIu64, (uint64_t)1792411260000ULL);
    check_str(buf, "BARVIS_START_MS=1792411260000");
    snprintf(buf, sizeof(buf), ATHENA_TAG_SESSION "%s", "3f9a0c11d2e4b507");
    check_str(buf, "BARVIS_SESSION=3f9a0c11d2e4b507");

    return checks_done("test_wire_ids");
}
