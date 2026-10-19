/* LilyGO T-Watch Ultra pin map.
 *
 * Sources: docs/t-watch-ultra-plan.md §1, LilyGoLib docs/hardware/lilygo-t-watch-ultra.md,
 * arduino-esp32 variants/lilygo_twatch_ultra/pins_arduino.h and the schematic
 * "T-Watch Ultra V1.0 SCH 25-07-24.pdf" (LilyGoLib/schematic). */
#pragma once

/* I2C bus shared by AXP2101, XL9555, PCF85063A, DRV2605, CST9217 touch, BHI260AP */
#define PIN_I2C_SDA 3
#define PIN_I2C_SCL 2

#define I2C_ADDR_AXP2101 0x34
#define I2C_ADDR_XL9555 0x20
#define I2C_ADDR_PCF85063A 0x51
#define I2C_ADDR_DRV2605 0x5A
#define I2C_ADDR_CST9217 0x1A  /* touch: not used, kept in reset */
#define I2C_ADDR_BHI260AP 0x28 /* sensor: rail off */

/* Interrupts */
#define PIN_PMU_IRQ 7 /* AXP2101 IRQ, active low, open drain */
#define PIN_RTC_IRQ 1 /* PCF85063A INT (unused) */
#define PIN_BOOT_BUTTON 0

/* PDM microphone (TDK T3902) */
#define PIN_MIC_CLK 17
#define PIN_MIC_DATA 18

/* SPI bus shared by the microSD, the LoRa radio and the NFC reader (plan §2.2) */
#define PIN_SPI_MOSI 34
#define PIN_SPI_MISO 33
#define PIN_SPI_SCK 35
#define PIN_SD_CS 21
#define PIN_LORA_CS 36  /* kept Hi-Z: LoRa rail (ALDO3) is off */
#define PIN_LORA_RST 47 /* kept Hi-Z */
#define PIN_NFC_CS 4    /* kept Hi-Z: NFC rail (DLDO1) is off */

/* AMOLED, CO5300 over QSPI */
#define PIN_DISP_CS 41
#define PIN_DISP_SCK 40
#define PIN_DISP_D0 38
#define PIN_DISP_D1 39
#define PIN_DISP_D2 42
#define PIN_DISP_D3 45
#define PIN_DISP_TE 6
#define PIN_DISP_RST 37
#define DISP_WIDTH 410  /* portrait, as worn */
#define DISP_HEIGHT 502
#define DISP_X_OFFSET 22 /* panel column 0x16 is the first visible column (LilyGoLib) */
#define DISP_Y_OFFSET 0

/* XL9555 I/O expander pins, as linear indices: port 0 = P00..P07 -> 0..7,
 * port 1 = P10..P17 -> 8..15.
 *
 * ---------------------------------------------------------------------------------
 * THE TWO CANDIDATE PIN MAPS (plan §2.1). The sources looked like they disagreed:
 *   LilyGoLib hardware page:  touch reset = "GPIO10", SD detect = "GPIO12"
 *   arduino-esp32 board file: EXPANDS_TOUCH_RST = 8, EXPANDS_SD_DET = 10
 * The schematic (U22, XL9555QF24) shows: P06 = M_EN (haptics), P07 = VCI_EN (display),
 * P10 = TP_RST, P11 = n.c., P12 = SD_DET, P13 = LORA_SEL (RF switch).
 * So the hardware page uses the chip's port names (P10, P12) and the board file uses
 * linear indices (8, 10): the same pins. The firmware uses the schematic reading below.
 * CONFIRMED on the owner's watch (2026-10-19): P12 (index 10) reads LOW with a card in and
 * HIGH with it out, while P14 (index 12) stays HIGH either way. The "other" reading (P12 =
 * touch reset, SD detect = P14) is wrong; the _ALT names below only document it.
 * --------------------------------------------------------------------------------- */
#define EXP_PIN_HAPTIC_EN 6  /* P06, M_EN: DRV2605 enable */
#define EXP_PIN_DISP_EN 7    /* P07, VCI_EN: AMOLED panel power enable */
#define EXP_PIN_TOUCH_RST 8  /* P10, TP_RST: CST9217 reset, held low (touch unused) */
#define EXP_PIN_SD_DET 10    /* P12, SD_DET: low = card inserted (schematic + board file) */
#define EXP_PIN_LORA_RF_SW 11 /* P13, LORA_SEL: RF switch, left as input (LoRa off) */
#define EXP_PIN_TOUCH_RST_ALT 10 /* "GPIO10" if the hardware page meant a linear index */
#define EXP_PIN_SD_DET_ALT 12    /* "GPIO12" if the hardware page meant a linear index */

/* Charger limits (plan §1, §9): LilyGO says charge below 500 mA (<= 0.5 C of 1100 mAh). */
#define BOARD_CHARGE_CURRENT_MA 400
