/* Host tests for the SY6970 register decoding (boards/t4_s3/board/sy6970_codec.c). */
#include "check.h"
#include "sy6970_codec.h"

static void test_voltages(void)
{
    CHECK_EQ(sy6970_decode_batv_mv(0x00), 2304);
    CHECK_EQ(sy6970_decode_batv_mv(0x5D), 2304 + 20 * 93); /* 4164 mV */
    CHECK_EQ(sy6970_decode_batv_mv(0x80 | 0x5D), 4164);    /* THERM_STAT bit ignored */
    bool good = false;
    CHECK_EQ(sy6970_decode_vbus_mv(0x80 | 24, &good), 5000);
    CHECK(good);
    CHECK_EQ(sy6970_decode_vbus_mv(0x00, &good), 2600);
    CHECK(!good);
    CHECK_EQ(sy6970_decode_ichgr_ma(10), 500);
}

static void test_status(void)
{
    /* USB host (001), fast charging (10), power good. */
    const sy6970_status_t s = sy6970_decode_status((1 << 5) | (2 << 3) | 0x04);
    CHECK_EQ(s.vbus_stat, 1);
    CHECK_EQ(s.chg, SY6970_CHG_FAST);
    CHECK(s.power_good);
    const sy6970_status_t n = sy6970_decode_status(0x00);
    CHECK_EQ(n.chg, SY6970_CHG_NONE);
    CHECK(!n.power_good);
}

static void test_ichg(void)
{
    CHECK_EQ(sy6970_encode_ichg(512, 0x00), 8);
    CHECK_EQ(sy6970_encode_ichg(500, 0x00), 7);         /* rounds down */
    CHECK_EQ(sy6970_encode_ichg(512, 0x80), 0x80 | 8);  /* EN_PUMPX kept */
    CHECK_EQ(sy6970_encode_ichg(99999, 0x00), 0x4F);    /* 5056 mA */
    CHECK_EQ(sy6970_encode_ichg(-5, 0x00), 0);
    CHECK_EQ(sy6970_decode_ichg_ma(0x20), 2048);        /* the chip's default */
}

static void test_percent(void)
{
    CHECK_EQ(lipo_percent_from_mv(3000), 0);
    CHECK_EQ(lipo_percent_from_mv(3300), 0);
    CHECK_EQ(lipo_percent_from_mv(4200), 100);
    CHECK_EQ(lipo_percent_from_mv(3870), 55);
    const int mid = lipo_percent_from_mv(3900);
    CHECK(mid > 55 && mid < 65);
    int last = -1;
    for (int mv = 3200; mv <= 4300; mv += 10) {
        const int p = lipo_percent_from_mv(mv);
        CHECK(p >= last);
        CHECK(p >= 0 && p <= 100);
        last = p;
    }
}

int main(void)
{
    test_voltages();
    test_status();
    test_ichg();
    test_percent();
    return checks_done("test_sy6970_codec");
}
