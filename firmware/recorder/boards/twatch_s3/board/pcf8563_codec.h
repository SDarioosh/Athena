/* NXP PCF8563 time-register codec (BCD <-> calendar fields). Pure C; host-tested.
 * Register layout (datasheet 8.4): 0x02 VL_seconds (bit 7 = VL, clock integrity not
 * guaranteed), 0x03 Minutes, 0x04 Hours (24 h), 0x05 Days, 0x06 Weekdays,
 * 0x07 Century_months (bit 7 = C), 0x08 Years.
 *
 * Century bit as SensorLib (LilyGO's library, the watch's factory firmware) uses it:
 * C = 0 is 20xx, C = 1 is 19xx. We only write 2000..2099, so C is always written 0. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define PCF8563_TIME_REG_FIRST 0x02
#define PCF8563_TIME_REG_COUNT 7

typedef struct {
    int year;   /* 1900..2099 when read, 2000..2099 when written */
    int month;  /* 1..12 */
    int day;    /* 1..31 */
    int hour;   /* 0..23 */
    int minute; /* 0..59 */
    int second; /* 0..59 */
} pcf8563_datetime_t;

/* Decode the 7 time registers. *voltage_low is set from the VL flag (time not trusted). */
void pcf8563_decode(const uint8_t regs[PCF8563_TIME_REG_COUNT], pcf8563_datetime_t *dt,
                    bool *voltage_low);

/* Encode fields into the 7 time registers with VL and C cleared. False if out of range. */
bool pcf8563_encode(const pcf8563_datetime_t *dt, uint8_t regs[PCF8563_TIME_REG_COUNT]);
