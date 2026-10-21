#include "sy6970_codec.h"

sy6970_status_t sy6970_decode_status(uint8_t reg0b)
{
    return (sy6970_status_t){
        .vbus_stat = (reg0b >> 5) & 0x07,
        .chg = (sy6970_chg_t)((reg0b >> 3) & 0x03),
        .power_good = (reg0b & 0x04) != 0,
    };
}

int sy6970_decode_batv_mv(uint8_t reg0e)
{
    return 2304 + 20 * (reg0e & 0x7F);
}

int sy6970_decode_sysv_mv(uint8_t reg0f)
{
    return 2304 + 20 * (reg0f & 0x7F);
}

int sy6970_decode_vbus_mv(uint8_t reg11, bool *good)
{
    if (good) {
        *good = (reg11 & 0x80) != 0;
    }
    return 2600 + 100 * (reg11 & 0x7F);
}

int sy6970_decode_ichgr_ma(uint8_t reg12)
{
    return 50 * (reg12 & 0x7F);
}

uint8_t sy6970_encode_ichg(int ma, uint8_t reg04_old)
{
    if (ma < 0) {
        ma = 0;
    }
    if (ma > SY6970_ICHG_MAX_MA) {
        ma = SY6970_ICHG_MAX_MA;
    }
    return (uint8_t)((reg04_old & 0x80) | ((ma / SY6970_ICHG_STEP_MA) & 0x7F));
}

int sy6970_decode_ichg_ma(uint8_t reg04)
{
    return SY6970_ICHG_STEP_MA * (reg04 & 0x7F);
}

int lipo_percent_from_mv(int mv)
{
    /* Typical 1-cell Li-Po open-circuit voltage vs. state of charge. */
    static const struct {
        int mv;
        int pct;
    } curve[] = {
        {3300, 0}, {3500, 3}, {3600, 8}, {3680, 15}, {3730, 25}, {3780, 35}, {3820, 45},
        {3870, 55}, {3930, 65}, {4000, 75}, {4070, 85}, {4140, 95}, {4180, 100},
    };
    const int n = (int)(sizeof(curve) / sizeof(curve[0]));
    if (mv <= curve[0].mv) {
        return 0;
    }
    if (mv >= curve[n - 1].mv) {
        return 100;
    }
    for (int i = 1; i < n; i++) {
        if (mv < curve[i].mv) {
            const int dv = curve[i].mv - curve[i - 1].mv;
            const int dp = curve[i].pct - curve[i - 1].pct;
            return curve[i - 1].pct + (mv - curve[i - 1].mv) * dp / dv;
        }
    }
    return 100;
}
