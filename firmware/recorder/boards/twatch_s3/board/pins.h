/* LilyGO T-Watch S3 pin map.
 *
 * Sources: LilyGO TTGO_TWatch_Library, branch t-watch-s3 (src/utilities.h, src/LilyGoLib.cpp,
 * extras/Setup212_LilyGo_T_Watch_S3.h) and the schematic "T_WATCH_S3.pdf" V1.4 in that repo.
 *
 * Power (AXP2101, schematic sheet 1 and LilyGoLib beginPower()):
 *   DC1    ESP32-S3 and the +3V3 rail (BMA423, PCF8563 I2C pull-ups, DRV2605 VDD, mic): never off
 *   ALDO1  RTC_3_3V: the PCF8563 (through a diode; a backup cell keeps it when off). Kept on.
 *   ALDO2  LCD_VDD: the backlight LED anode. GPIO45 switches the LED's cathode transistor.
 *   ALDO3  AVDD: the ST7789's logic supply and the touch controller
 *   ALDO4  LoRa (SX1262), and GPS_VDD on boards with the GPS shield
 *   BLDO1  GPS on the version with BOOT and RST buttons on the back cover
 *   BLDO2  DRV2605 EN (the "LDO5" net): haptics enable
 *   DC3    GPS on earlier versions (without the back buttons)
 *   DLDO1  speaker amplifier (MAX98357A)
 *
 * The only button on the outside is the side key: the AXP2101's PWRON. Some versions have
 * BOOT (GPIO0) and RST under the back cover; the firmware doesn't use BOOT. */
#pragma once

/* I2C bus shared by AXP2101, PCF8563, DRV2605 and BMA423 */
#define PIN_I2C_SDA 10
#define PIN_I2C_SCL 11

#define I2C_ADDR_AXP2101 0x34
#define I2C_ADDR_PCF8563 0x51
#define I2C_ADDR_DRV2605 0x5A
#define I2C_ADDR_BMA423 0x19 /* accelerometer: not used */

/* Interrupts */
#define PIN_PMU_IRQ 21   /* AXP2101 IRQ, active low, open drain; an RTC GPIO (deep-sleep wake) */
#define PIN_RTC_IRQ 17   /* PCF8563 INT (unused) */
#define PIN_BMA_IRQ 14   /* BMA423 INT1 (unused) */
#define PIN_TOUCH_IRQ 16 /* FT6336 INT (unused; the touch bus is GPIO39/40) */

/* PDM microphone (Knowles SPM1423) */
#define PIN_MIC_CLK 44
#define PIN_MIC_DATA 47

/* LCD: ST7789, 240x240 over SPI. No reset line (an RC on the panel's RST resets it at
 * power-up; the driver also sends a software reset). */
#define PIN_DISP_MOSI 13
#define PIN_DISP_SCK 18
#define PIN_DISP_CS 12
#define PIN_DISP_DC 38
#define PIN_DISP_BL 45 /* backlight: high = on. A strapping pin (VDD_SPI) with a 10k pull-down. */
#define DISP_WIDTH 240
#define DISP_HEIGHT 240
/* TFT_eSPI rotation 2 (LilyGoLib setRotation(2)): both axes mirrored, and the 240 visible
 * rows are the last 240 of the controller's 320. Colors inverted (TFT_INVERSION_ON). */
#define DISP_Y_OFFSET 80

/* SX1262 LoRa: its rail (ALDO4) is off; the pins stay Hi-Z. */
#define PIN_LORA_CS 5
#define PIN_LORA_RST 8

/* Charger: the watch's cell is 470 mAh; 200 mA is under 0.5 C (LilyGoLib uses 125 mA). */
#define BOARD_CHARGE_CURRENT_MA 200
