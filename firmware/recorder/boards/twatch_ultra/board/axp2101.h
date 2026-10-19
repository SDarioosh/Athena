/* Minimal X-Powers AXP2101 driver: rails, charger, fuel gauge, power-button IRQ.
 * Register map from the AXP2101 datasheet (V1.4); cross-checked against XPowersLib (MIT). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef enum {
    AXP_DC1, AXP_DC2, AXP_DC3, AXP_DC4, AXP_DC5,
    AXP_ALDO1, AXP_ALDO2, AXP_ALDO3, AXP_ALDO4,
    AXP_BLDO1, AXP_BLDO2, AXP_CPUSLDO, AXP_DLDO1, AXP_DLDO2,
    AXP_RAIL_COUNT,
} axp_rail_t;

/* IRQ bits as one 24-bit word: INTEN1/INTSTS1 in bits 0-7, INTEN2/INTSTS2 in 8-15,
 * INTEN3/INTSTS3 in 16-23. */
#define AXP_IRQ_PKEY_POSITIVE (1u << 8)
#define AXP_IRQ_PKEY_NEGATIVE (1u << 9)
#define AXP_IRQ_PKEY_LONG (1u << 10)
#define AXP_IRQ_PKEY_SHORT (1u << 11)
#define AXP_IRQ_BAT_REMOVE (1u << 12)
#define AXP_IRQ_BAT_INSERT (1u << 13)
#define AXP_IRQ_VBUS_REMOVE (1u << 14)
#define AXP_IRQ_VBUS_INSERT (1u << 15)
#define AXP_IRQ_CHG_START (1u << 19)
#define AXP_IRQ_CHG_DONE (1u << 20)

typedef struct {
    bool battery_present;
    bool vbus_good;
    bool charging;
    int millivolts;
    int percent;
} axp_battery_t;

esp_err_t axp2101_init(uint8_t i2c_addr);

esp_err_t axp2101_rail_enable(axp_rail_t rail, bool on);
bool axp2101_rail_is_enabled(axp_rail_t rail);
/* LDO rails only (ALDO1-4, BLDO1-2, DLDO1): 500..3500 mV in 100 mV steps. */
esp_err_t axp2101_rail_set_mv(axp_rail_t rail, int mv);
int axp2101_rail_get_mv(axp_rail_t rail);
const char *axp2101_rail_name(axp_rail_t rail);

/* Charger: constant current (rounded down to a supported step) and 4.2 V target. */
esp_err_t axp2101_set_charge_current_ma(int ma);
int axp2101_get_charge_current_ma(void);
esp_err_t axp2101_set_charge_voltage_4v2(void);
esp_err_t axp2101_chgled_off(void);
/* Charge the RTC backup cell (VBACKUP) to `mv` (2600..3300, 100 mV steps). */
esp_err_t axp2101_backup_charge_enable(int mv);
/* Enable only the ADC channels we need: VBAT (bit 0) and the TS thermistor (bit 1). */
esp_err_t axp2101_adc_enable(uint8_t channel_mask);

esp_err_t axp2101_read_battery(axp_battery_t *out);

esp_err_t axp2101_irq_enable(uint32_t mask);
/* Read all three IRQ status registers and clear the bits that were set. */
esp_err_t axp2101_irq_read_clear(uint32_t *status);

/* Load the 128-byte battery model into the fuel gauge and verify it. Resets the gauge. */
esp_err_t axp2101_gauge_write_params(const uint8_t *params, size_t len);

/* Log the raw registers that matter: rail enables and voltages, charger, LED, ADC, keys. */
void axp2101_log_registers(void);
