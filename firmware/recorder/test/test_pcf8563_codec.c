/* Host tests for the T-Watch S3's PCF8563 codec (boards/twatch_s3/board/pcf8563_codec.c). */
#include "check.h"
#include "pcf8563_codec.h"

int main(void)
{
    pcf8563_datetime_t dt = {2026, 10, 20, 17, 5, 9};
    uint8_t regs[PCF8563_TIME_REG_COUNT];
    CHECK(pcf8563_encode(&dt, regs));
    CHECK_EQ(regs[0], 0x09); /* seconds, VL clear */
    CHECK_EQ(regs[1], 0x05);
    CHECK_EQ(regs[2], 0x17);
    CHECK_EQ(regs[3], 0x20);
    CHECK_EQ(regs[4], 2);    /* 2026-10-20 is a Tuesday */
    CHECK_EQ(regs[5], 0x10); /* October, C = 0 */
    CHECK_EQ(regs[6], 0x26);

    pcf8563_datetime_t out;
    bool low = true;
    pcf8563_decode(regs, &out, &low);
    CHECK(!low);
    CHECK_EQ(out.year, 2026);
    CHECK_EQ(out.month, 10);
    CHECK_EQ(out.day, 20);
    CHECK_EQ(out.hour, 17);
    CHECK_EQ(out.minute, 5);
    CHECK_EQ(out.second, 9);

    /* VL set (the clock lost power): time decodes but is marked untrusted. */
    regs[0] |= 0x80;
    pcf8563_decode(regs, &out, &low);
    CHECK(low);
    CHECK_EQ(out.second, 9);

    /* Century bit set: 19xx, as SensorLib reads it. */
    regs[5] |= 0x80;
    pcf8563_decode(regs, &out, NULL);
    CHECK_EQ(out.year, 1926);
    CHECK_EQ(out.month, 10);

    /* Unused high bits are ignored. */
    const uint8_t noisy[7] = {0x00, 0x80 | 0x59, 0xC0 | 0x23, 0xC0 | 0x31, 0xF8 | 0x6, 0x60 | 0x12, 0x99};
    pcf8563_decode(noisy, &out, NULL);
    CHECK_EQ(out.minute, 59);
    CHECK_EQ(out.hour, 23);
    CHECK_EQ(out.day, 31);
    CHECK_EQ(out.month, 12);
    CHECK_EQ(out.year, 2099);

    pcf8563_datetime_t bad = {1999, 12, 31, 0, 0, 0};
    CHECK(!pcf8563_encode(&bad, regs));
    bad = (pcf8563_datetime_t){2100, 1, 1, 0, 0, 0};
    CHECK(!pcf8563_encode(&bad, regs));
    bad = (pcf8563_datetime_t){2026, 13, 1, 0, 0, 0};
    CHECK(!pcf8563_encode(&bad, regs));

    return checks_done("test_pcf8563_codec");
}
