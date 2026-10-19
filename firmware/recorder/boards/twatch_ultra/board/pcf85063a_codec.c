#include "pcf85063a_codec.h"

uint8_t pcf85063a_bcd_encode(int value)
{
    return (uint8_t)(((value / 10) << 4) | (value % 10));
}

int pcf85063a_bcd_decode(uint8_t bcd)
{
    return (bcd >> 4) * 10 + (bcd & 0x0F);
}

void pcf85063a_decode(const uint8_t regs[PCF85063A_TIME_REG_COUNT], pcf85063a_datetime_t *dt,
                      bool *osc_stopped)
{
    if (osc_stopped) {
        *osc_stopped = (regs[0] & 0x80) != 0;
    }
    dt->second = pcf85063a_bcd_decode(regs[0] & 0x7F);
    dt->minute = pcf85063a_bcd_decode(regs[1] & 0x7F);
    dt->hour = pcf85063a_bcd_decode(regs[2] & 0x3F);
    dt->day = pcf85063a_bcd_decode(regs[3] & 0x3F);
    /* regs[4] is the weekday; derived, not stored. */
    dt->month = pcf85063a_bcd_decode(regs[5] & 0x1F);
    dt->year = 2000 + pcf85063a_bcd_decode(regs[6]);
}

static int weekday(int year, int month, int day)
{
    /* Sakamoto's algorithm: 0 = Sunday, matching the PCF85063A weekday register. */
    static const int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    year -= month < 3;
    return (year + year / 4 - year / 100 + year / 400 + t[month - 1] + day) % 7;
}

bool pcf85063a_encode(const pcf85063a_datetime_t *dt, uint8_t regs[PCF85063A_TIME_REG_COUNT])
{
    if (dt->year < 2000 || dt->year > 2099 || dt->month < 1 || dt->month > 12 || dt->day < 1 ||
        dt->day > 31 || dt->hour < 0 || dt->hour > 23 || dt->minute < 0 || dt->minute > 59 ||
        dt->second < 0 || dt->second > 59) {
        return false;
    }
    regs[0] = pcf85063a_bcd_encode(dt->second); /* OS bit cleared */
    regs[1] = pcf85063a_bcd_encode(dt->minute);
    regs[2] = pcf85063a_bcd_encode(dt->hour);
    regs[3] = pcf85063a_bcd_encode(dt->day);
    regs[4] = (uint8_t)weekday(dt->year, dt->month, dt->day);
    regs[5] = pcf85063a_bcd_encode(dt->month);
    regs[6] = pcf85063a_bcd_encode(dt->year - 2000);
    return true;
}
