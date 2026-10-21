/* Board layer interface (docs/t-watch-ultra-plan.md §6).
 *
 * Everything the shared firmware needs from a board goes through this header: power
 * rails, battery, wall-clock time, buttons, haptics, display, storage and the mic pins.
 * Each board implements it in boards/<name>/board/: the T-Watch Ultra, the T-Watch S3
 * (one button, no SD card: storage is a FAT partition in its flash) and the T4-S3
 * conference recorder (one button, four I2S mics, no haptics, no RTC chip). The XIAO ESP32-S3
 * Sense pendant will implement a subset (no display, no rails) and return
 * ESP_ERR_NOT_SUPPORTED for what it lacks.
 *
 * Later milestones add to this file rather than reaching into board code: a "phone sync"
 * status for the screen.
 *
 * Sleep: light sleep is automatic (esp_pm), so board_init() must arm its buttons as GPIO
 * wake sources and nothing in the board layer may poll while idle. Standby (deep sleep)
 * goes through board_standby_prepare().
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "battery_guard.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Switchable power rails. Rails a board doesn't have return ESP_ERR_NOT_SUPPORTED. */
typedef enum {
    BOARD_RAIL_SD,      /* storage card */
    BOARD_RAIL_DISPLAY, /* screen panel */
    BOARD_RAIL_GPS,
    BOARD_RAIL_LORA,
    BOARD_RAIL_NFC,
    BOARD_RAIL_SPEAKER,
    BOARD_RAIL_SENSOR,
    BOARD_RAIL_COUNT,
} board_rail_t;

typedef struct {
    bool present;    /* a battery is connected */
    int millivolts;  /* terminal voltage */
    int percent;     /* fuel-gauge state of charge, -1 if unknown */
    bool charging;   /* charger is pushing current in */
    bool vbus;       /* USB power present */
} board_battery_t;

typedef enum {
    BOARD_BUTTON_PWR,  /* power-chip button (short/long press from the PMIC) */
    BOARD_BUTTON_BOOT, /* GPIO button */
} board_button_t;

/* GPIO buttons report SHORT (a single press, after the double-press window has passed),
 * DOUBLE and LONG (while still held), see button_gesture.h. The PMIC button reports SHORT
 * and LONG only (the PMIC's own long-press time, ~1 s). */
typedef enum {
    BOARD_PRESS_SHORT,
    BOARD_PRESS_LONG,
    BOARD_PRESS_DOUBLE,
} board_press_t;

/* Called from the board's button task, never from an ISR. Keep it short: post an event. */
typedef void (*board_button_cb_t)(board_button_t button, board_press_t press, void *ctx);

/* How the board's mics are wired. PDM: one mic, clk + data (T-Watch Ultra, T-Watch S3).
 * I2S: standard (Philips) I2S mics such as the INMP441, 24-bit in 32-bit slots, in stereo
 * pairs: both mics of a pair share one data line, one with L/R tied low (left slot), the
 * other tied high (right slot). Every pair shares one bit clock and word select: I2S0
 * drives them, I2S1 (the second pair, data2_gpio) listens to the same clock as a slave, so
 * all four mics are sampled on the same edges (a mic array). data2_gpio = -1: one pair. */
typedef enum {
    BOARD_MIC_PDM = 0,
    BOARD_MIC_I2S,
} board_mic_kind_t;

typedef struct {
    board_mic_kind_t kind;
    int clk_gpio;   /* PDM clock, or the I2S bit clock (BCLK/SCK) */
    int data_gpio;  /* PDM data, or the first pair's I2S data (SD) */
    int ws_gpio;    /* I2S word select (WS/LRCL); unused for PDM */
    int data2_gpio; /* I2S second pair's data, -1 if none; unused for PDM */
} board_mic_pins_t;

/* One peaking-EQ band that corrects the board's mic acoustics (port, case cavity). */
typedef struct {
    float hz;
    float gain_db;
    float q;
} board_mic_eq_band_t;

typedef struct {
    int width;
    int height;
} board_display_info_t;

/* ---- identity and bring-up ---- */
const char *board_name(void);

/* Bring up buses and chips, switch every unused rail off, log the read-back. Must be
 * called first. Returns an error only if a chip we need is missing. */
esp_err_t board_init(void);

/* ---- power ---- */
esp_err_t board_rail_set(board_rail_t rail, bool on);
/* Read back from the power chip, not from a cached flag. */
bool board_rail_is_on(board_rail_t rail);
/* Log every rail (read back) and the charger configuration. */
void board_log_power(void);
/* Log the I/O expander's input levels, for the pin-map check (pins.h). */
void board_log_expander(void);

esp_err_t board_battery_read(board_battery_t *out);
/* When the battery counts as too low to record (main/battery_guard.c applies them). */
void board_battery_limits(bg_limits_t *out);

/* Standby: arm the board's deep-sleep wake sources (its power button, USB power) and leave
 * its chips in their lowest state. The caller then calls esp_deep_sleep_start(); waking is
 * a reset with a wakeup cause other than ESP_SLEEP_WAKEUP_UNDEFINED. Fails, leaving the
 * board as it was, if a wake source is already active (the chip would wake at once). */
esp_err_t board_standby_prepare(void);

/* ---- wall-clock time (UTC epoch seconds) ---- */
/* *valid is false if the RTC lost power since it was last set. */
esp_err_t board_rtc_get(int64_t *epoch, bool *valid);
esp_err_t board_rtc_set(int64_t epoch);

/* ---- buttons and haptics ---- */
void board_buttons_set_callback(board_button_cb_t cb, void *ctx);
/* Live level of a button. Only GPIO buttons can answer; the PWR button reports false. */
bool board_button_is_down(board_button_t button);
/* Keep the screen on for the whole recording, showing the time (ui_show_recording). For a
 * desk recorder on USB (T4-S3); the watches light it only for a few seconds per action. */
bool board_screen_on_while_recording(void);
/* One short line for the status screen saying what the buttons do, e.g. "PWR: REC  2X BOOT: MUTE". */
const char *board_button_hint(void);
/* `count` short buzzes (1 = started, 2 = stopped or muted). Blocks for the duration. */
esp_err_t board_haptic_pulse(int count);
/* One long buzz (~0.5 s; unmuted). Blocks for the duration. */
esp_err_t board_haptic_long(void);

/* ---- microphone ---- */
board_mic_pins_t board_mic_pins(void);
/* EQ bands for this board's mic, measured on the device; *bands may be NULL (count 0). */
int board_mic_eq(const board_mic_eq_band_t **bands);

/* ---- storage (card powered only while mounted) ---- */
/* Always true for built-in storage (a flash partition). */
bool board_storage_card_present(void);
/* "SD" or "FLASH": what the status screen calls the storage. */
const char *board_storage_label(void);
/* Recording stops when less than this is free (room for FAT, the power log, a last chunk). */
uint64_t board_storage_reserve_bytes(void);
/* Power the card rail, mount FAT. *mount_point is e.g. "/sdcard". */
esp_err_t board_storage_mount(const char **mount_point);
esp_err_t board_storage_unmount(void);
bool board_storage_is_mounted(void);
esp_err_t board_storage_info(uint64_t *total_bytes, uint64_t *free_bytes);

/* ---- display (powered only between on and off) ---- */
esp_err_t board_display_on(board_display_info_t *info);
/* RGB565 in panel byte order (textfb_rgb()), row-major, w*h pixels. Blocks until sent. */
esp_err_t board_display_draw(int x, int y, int w, int h, const uint16_t *pixels);
/* 0..100. The panel comes up dark; call this after the first frame is drawn. */
esp_err_t board_display_set_brightness(int percent);
esp_err_t board_display_off(void);

#ifdef __cplusplus
}
#endif
