/* LilyGO T4-S3 pin map (2.41" AMOLED, ESP32-S3R8, 16 MB flash, 8 MB octal PSRAM).
 *
 * Sources: LilyGO LilyGo-AMOLED-Series (src/LilyGo_AMOLED.h, BOARD_AMOLED_241, and
 * src/LilyGo_AMOLED.cpp beginAMOLED_241()), the T4-S3 wiki page (wiki.lilygo.cc,
 * products/t4-series/t4-s3) and its pinout picture. Checked on the owner's board
 * (2026-10-21): ESP32-S3 rev 0.2, 8 MB PSRAM, 16 MB flash; the I2C scan is in the boot log.
 *
 * Power: a slide switch on the battery line; the SY6970 charger (I2C 0x6A) charges the
 * Li-Po from USB-C and feeds the 3V3 regulator. There is no power-management chip with
 * switchable rails: only the AMOLED's own power IC has an enable pin (GPIO9).
 *
 * Buttons: BOOT (GPIO0, active low, pulled up on the board) is the only one the firmware
 * uses; RST resets the chip. No haptics, no buzzer, no RTC chip.
 *
 * Mic array: four INMP441 I2S mics on the 30-pin header (see "Mic array" below). */
#pragma once

/* I2C: SY6970 charger, CST226SE touch, and both Qwiic connectors */
#define PIN_I2C_SDA 6
#define PIN_I2C_SCL 7
#define I2C_ADDR_SY6970 0x6A
#define I2C_ADDR_CST226 0x5A /* touch: not used */

#define PIN_PMU_IRQ 5    /* SY6970 INT: a 256 us low pulse on a charger change (unused) */
#define PIN_TOUCH_IRQ 8  /* unused */
#define PIN_TOUCH_RST 17 /* unused, left Hi-Z */

/* BOOT button: low = pressed. An RTC GPIO, so it also wakes the chip from deep sleep. */
#define PIN_BUTTON 0

/* AMOLED: RM690B0, 450x600 over QSPI (opcode 0x02 for commands, 0x32 for pixels, as the
 * CO5300; esp_lcd_co5300 drives it with the RM690B0 init list in display.c). */
#define PIN_DISP_CS 11
#define PIN_DISP_SCK 15
#define PIN_DISP_D0 14
#define PIN_DISP_D1 10
#define PIN_DISP_D2 16
#define PIN_DISP_D3 12
#define PIN_DISP_RST 13
#define PIN_DISP_TE 18  /* tearing effect: unused, left Hi-Z (wiki: don't use it for anything) */
#define PIN_DISP_EN 9   /* the AMOLED power IC (LilyGO "PMICEn"): high = panel powered */
/* Portrait, the panel's native scan direction. Its RAM is 16 columns wider than the glass
 * (LilyGO setRotation: offset 16 on the 450 side). */
#define DISP_WIDTH 450
#define DISP_HEIGHT 600
#define DISP_X_OFFSET 16
#define DISP_Y_OFFSET 0

/* microSD slot (SPI): not used yet, recordings go to flash. Left Hi-Z. */
#define PIN_SD_CS 1
#define PIN_SD_MOSI 2
#define PIN_SD_SCK 3
#define PIN_SD_MISO 4

/* ---- Mic array: 4 x INMP441 on the 30-pin header ----
 *
 * All four share BCLK (their SCK) and WS. Two data lines, each shared by a pair:
 *
 *   mic   L/R pin   SD (data) to   slot
 *   A-L   GND       GPIO41         pair A left  (channel 0)
 *   A-R   3V3       GPIO41         pair A right (channel 1)
 *   B-L   GND       GPIO42         pair B left  (channel 2)
 *   B-R   3V3       GPIO42         pair B right (channel 3)
 *
 *   every mic: SCK -> GPIO39, WS -> GPIO40, VDD -> 3V3, GND -> GND
 *
 * GPIO38..42 are plain I/O on the header (39..42 are also JTAG, which the firmware doesn't
 * use: it debugs over the USB serial console). The firmware pulls each data line down
 * (the INMP441 data sheet asks for 100k to GND), so a missing mic reads as silence and is
 * left out of the mix. Fewer than four mics work: one pair on GPIO41 alone is fine. */
#define PIN_MIC_BCLK 39
#define PIN_MIC_WS 40
#define PIN_MIC_DATA_A 41
#define PIN_MIC_DATA_B 42

/* Charger: the battery's size isn't known; 512 mA suits a 1000 mAh cell or larger at
 * 0.5 C. The SY6970's own default is 2048 mA. */
#define BOARD_CHARGE_CURRENT_MA 512
