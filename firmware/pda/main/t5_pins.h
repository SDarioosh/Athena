/* LilyGO T5 E-Paper Pro (H752-01, schematic V1.0 24-12-24): pins and I2C addresses.
 *
 * From LilyGO's docs/pinmap.md and examples/factory/main/utilities.h in
 * github.com/Xinyuan-LilyGO/T5S3-4.7-e-paper-PRO, checked on the owner's board at bring-up
 * (docs/t5-pda-plan.md §1). The e-paper data and control lines (GPIO 4-8, 15-18, 41, 42, 45,
 * 48) are epdiy's board "v7" layout and are driven by epdiy, not listed here. */
#pragma once

/* One I2C bus for every chip: PCA9535, TPS65185 (epdiy), GT911, PCF8563, BQ27220, BQ25896. */
#define T5_I2C_SDA 39
#define T5_I2C_SCL 40

#define T5_PCA9535_INT 38 /* taken by epdiy (TPS interrupt) */
#define T5_RTC_INT 2
#define T5_TOUCH_INT 3
#define T5_TOUCH_RST 9
#define T5_BOOT_BTN 0     /* active low; low at reset = download mode */
#define T5_BACKLIGHT 11   /* front light LED driver enable (PWM); kept off */

/* SD card / LoRa SPI: not used in v1 (plan §7: the feed never goes to the card). */
#define T5_SD_CS 12
#define T5_LORA_CS 46

/* 7-bit I2C addresses */
#define T5_ADDR_PCA9535 0x20
#define T5_ADDR_RTC 0x51     /* PCF8563 */
#define T5_ADDR_GAUGE 0x55   /* BQ27220 */
#define T5_ADDR_TOUCH 0x5D   /* GT911 (0x14 if INT was high at reset) */
#define T5_ADDR_TOUCH_ALT 0x14
#define T5_ADDR_TPS 0x68     /* TPS65185 (epdiy) */
#define T5_ADDR_CHARGER 0x6B /* BQ25896 */

/* PCA9535: port 0 bit 0 = LoRa/GPS rail enable; port 1 = e-paper control (epdiy) and the
 * side button (IO1_2, active low). */
#define T5_PCA_P0_LORA_EN 0x01
#define T5_PCA_P1_BUTTON 0x04
#define T5_PCA_P1_TPS_PWRGOOD 0x40
#define T5_PCA_P1_TPS_INT 0x80
