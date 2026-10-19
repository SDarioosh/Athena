/* PCF85063A time-register codec (BCD <-> calendar fields). Pure C; host-tested.
 * Register layout (datasheet 7.5): 0x04 Seconds (bit 7 = OS, oscillator stopped),
 * 0x05 Minutes, 0x06 Hours (24 h mode), 0x07 Days, 0x08 Weekdays, 0x09 Months, 0x0A Years. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define PCF85063A_TIME_REG_FIRST 0x04
#define PCF85063A_TIME_REG_COUNT 7

typedef struct {
    int year;   /* 2000..2099 */
    int month;  /* 1..12 */
    int day;    /* 1..31 */
    int hour;   /* 0..23 */
    int minute; /* 0..59 */
    int second; /* 0..59 */
} pcf85063a_datetime_t;

uint8_t pcf85063a_bcd_encode(int value);
int pcf85063a_bcd_decode(uint8_t bcd);

/* Decode the 7 time registers. *osc_stopped is set from the OS flag (time not trusted). */
void pcf85063a_decode(const uint8_t regs[PCF85063A_TIME_REG_COUNT], pcf85063a_datetime_t *dt,
                      bool *osc_stopped);

/* Encode fields into the 7 time registers with OS cleared. Returns false if out of range. */
bool pcf85063a_encode(const pcf85063a_datetime_t *dt, uint8_t regs[PCF85063A_TIME_REG_COUNT]);
