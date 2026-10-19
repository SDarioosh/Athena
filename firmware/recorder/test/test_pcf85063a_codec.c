#include "check.h"
#include "pcf85063a_codec.h"

int main(void)
{
    CHECK_EQ(pcf85063a_bcd_encode(0), 0x00);
    CHECK_EQ(pcf85063a_bcd_encode(59), 0x59);
    CHECK_EQ(pcf85063a_bcd_decode(0x59), 59);
    CHECK_EQ(pcf85063a_bcd_decode(0x07), 7);

    pcf85063a_datetime_t dt = {2026, 10, 19, 12, 34, 56};
    uint8_t regs[PCF85063A_TIME_REG_COUNT];
    CHECK(pcf85063a_encode(&dt, regs));
    CHECK_EQ(regs[0], 0x56); /* seconds, OS clear */
    CHECK_EQ(regs[1], 0x34);
    CHECK_EQ(regs[2], 0x12);
    CHECK_EQ(regs[3], 0x19);
    CHECK_EQ(regs[4], 1); /* 2026-10-19 is a Monday */
    CHECK_EQ(regs[5], 0x10);
    CHECK_EQ(regs[6], 0x26);

    pcf85063a_datetime_t out;
    bool stopped = true;
    pcf85063a_decode(regs, &out, &stopped);
    CHECK(!stopped);
    CHECK_EQ(out.year, 2026);
    CHECK_EQ(out.month, 10);
    CHECK_EQ(out.day, 19);
    CHECK_EQ(out.hour, 12);
    CHECK_EQ(out.minute, 34);
    CHECK_EQ(out.second, 56);

    /* OS flag set (battery was flat): time decodes but is marked untrusted. */
    regs[0] |= 0x80;
    pcf85063a_decode(regs, &out, &stopped);
    CHECK(stopped);
    CHECK_EQ(out.second, 56);

    /* Unused high bits in the registers are ignored. */
    const uint8_t noisy[7] = {0x00, 0x80 | 0x00, 0xC0 | 0x23, 0xC0 | 0x01, 0xF8 | 0x4, 0xE0 | 0x01, 0x00};
    pcf85063a_decode(noisy, &out, NULL);
    CHECK_EQ(out.hour, 23);
    CHECK_EQ(out.day, 1);
    CHECK_EQ(out.month, 1);
    CHECK_EQ(out.year, 2000);

    pcf85063a_datetime_t bad = {1999, 1, 1, 0, 0, 0};
    CHECK(!pcf85063a_encode(&bad, regs));
    bad = (pcf85063a_datetime_t){2026, 10, 19, 0, 60, 0};
    CHECK(!pcf85063a_encode(&bad, regs));

    /* Sunday check for the weekday register. */
    dt = (pcf85063a_datetime_t){2026, 10, 18, 0, 0, 0};
    CHECK(pcf85063a_encode(&dt, regs));
    CHECK_EQ(regs[4], 0);

    return checks_done("test_pcf85063a_codec");
}
