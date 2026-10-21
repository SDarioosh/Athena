#include "pcf8563_codec.h"

static uint8_t bcd_encode(int value)
{
    return (uint8_t)(((value / 10) << 4) | (value % 10));
}

static int bcd_decode(uint8_t bcd)
{
    return (bcd >> 4) * 10 + (bcd & 0x0F);
}

void pcf8563_decode(const uint8_t regs[PCF8563_TIME_REG_COUNT], pcf8563_datetime_t *dt,
                    bool *voltage_low)
{
    if (voltage_low) {
        *voltage_low = (regs[0] & 0x80) != 0;
    }
    dt->second = bcd_decode(regs[0] & 0x7F);
    dt->minute = bcd_decode(regs[1] & 0x7F);
    dt->hour = bcd_decode(regs[2] & 0x3F);
    dt->day = bcd_decode(regs[3] & 0x3F);
    /* regs[4] is the weekday; derived, not stored. */
    dt->month = bcd_decode(regs[5] & 0x1F);
    dt->year = ((regs[5] & 0x80) ? 1900 : 2000) + bcd_decode(regs[6]);
}

static int weekday(int year, int month, int day)
{
    /* Sakamoto's algorithm: 0 = Sunday, as the PCF8563 weekday register counts. */
    static const int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    year -= month < 3;
    return (year + year / 4 - year / 100 + year / 400 + t[month - 1] + day) % 7;
}

bool pcf8563_encode(const pcf8563_datetime_t *dt, uint8_t regs[PCF8563_TIME_REG_COUNT])
{
    if (dt->year < 2000 || dt->year > 2099 || dt->month < 1 || dt->month > 12 || dt->day < 1 ||
        dt->day > 31 || dt->hour < 0 || dt->hour > 23 || dt->minute < 0 || dt->minute > 59 ||
        dt->second < 0 || dt->second > 59) {
        return false;
    }
    regs[0] = bcd_encode(dt->second); /* VL cleared */
    regs[1] = bcd_encode(dt->minute);
    regs[2] = bcd_encode(dt->hour);
    regs[3] = bcd_encode(dt->day);
    regs[4] = (uint8_t)weekday(dt->year, dt->month, dt->day);
    regs[5] = bcd_encode(dt->month); /* C = 0: 20xx */
    regs[6] = bcd_encode(dt->year - 2000);
    return true;
}
