/* SY6970 charger register decoding, and a Li-Po charge estimate from the voltage (the
 * SY6970 has no fuel gauge). Pure C; host-tested (test/test_sy6970_codec.c).
 *
 * The SY6970 is register-compatible with TI's BQ25895 for everything used here:
 *   REG02 bit7 CONV_START (one-shot ADC), bit6 CONV_RATE (continuous)
 *   REG03 bit4 CHG_CONFIG (charge enable), bit5 OTG_CONFIG (boost)
 *   REG04 [6:0] ICHG, 64 mA per step
 *   REG07 bit6 STAT_DIS (the charge LED), [5:4] WATCHDOG (00 = off)
 *   REG0B [7:5] VBUS_STAT, [4:3] CHRG_STAT, bit2 PG_STAT
 *   REG0E [6:0] BATV = 2304 mV + 20 mV * n
 *   REG11 bit7 VBUS_GD, [6:0] VBUSV = 2600 mV + 100 mV * n
 *   REG12 [6:0] ICHGR = 50 mA * n (charge current now)
 *   REG14 [5:3] PN (part number), [1:0] DEV_REV */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SY6970_REG_INPUT 0x00
#define SY6970_REG_ADC 0x02
#define SY6970_REG_CONFIG 0x03
#define SY6970_REG_ICHG 0x04
#define SY6970_REG_TIMER 0x07
#define SY6970_REG_STATUS 0x0B
#define SY6970_REG_FAULT 0x0C
#define SY6970_REG_BATV 0x0E
#define SY6970_REG_SYSV 0x0F
#define SY6970_REG_VBUSV 0x11
#define SY6970_REG_ICHGR 0x12
#define SY6970_REG_ID 0x14

#define SY6970_ADC_CONV_START 0x80
#define SY6970_ADC_CONV_RATE 0x40
#define SY6970_CONFIG_OTG 0x20
#define SY6970_CONFIG_CHG 0x10
#define SY6970_TIMER_STAT_DIS 0x40
#define SY6970_TIMER_WATCHDOG_MASK 0x30

#define SY6970_FAULT_BAT 0x08 /* REG0C bit3: battery over-voltage */

#define SY6970_ICHG_STEP_MA 64
#define SY6970_ICHG_MAX_MA 5056

typedef enum {
    SY6970_CHG_NONE = 0,
    SY6970_CHG_PRE = 1,
    SY6970_CHG_FAST = 2,
    SY6970_CHG_DONE = 3,
} sy6970_chg_t;

typedef struct {
    int vbus_stat;     /* REG0B[7:5]: 0 none, 1 USB host, 2 adapter, 7 OTG */
    sy6970_chg_t chg;  /* REG0B[4:3] */
    bool power_good;   /* REG0B bit2 */
} sy6970_status_t;

sy6970_status_t sy6970_decode_status(uint8_t reg0b);
int sy6970_decode_batv_mv(uint8_t reg0e);
int sy6970_decode_sysv_mv(uint8_t reg0f);
/* *good = VBUS_GD. */
int sy6970_decode_vbus_mv(uint8_t reg11, bool *good);
int sy6970_decode_ichgr_ma(uint8_t reg12);
/* REG04 value for a charge current, rounded down to the 64 mA step; clamped to the range. */
uint8_t sy6970_encode_ichg(int ma, uint8_t reg04_old);
int sy6970_decode_ichg_ma(uint8_t reg04);

/* State of charge of a 1-cell Li-Po (4.2 V full) from its resting voltage, 0..100. A rough
 * curve: under load or while charging the voltage reads a few % off. */
int lipo_percent_from_mv(int mv);

#ifdef __cplusplus
}
#endif
