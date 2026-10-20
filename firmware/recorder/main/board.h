/* Board layer interface (docs/t-watch-ultra-plan.md §6).
 *
 * Everything the shared firmware needs from a board goes through this header: power
 * rails, battery, wall-clock time, buttons, haptics, display, storage and the mic pins.
 * Each board implements it in boards/<name>/board/. The T-Watch Ultra is the first; the
 * XIAO ESP32-S3 Sense pendant will implement a subset (no display, no rails) and return
 * ESP_ERR_NOT_SUPPORTED for what it lacks.
 *
 * Later milestones add to this file rather than reaching into board code: a low-battery
 * threshold hook, a "phone sync" status for the screen.
 *
 * Sleep: light sleep is automatic (esp_pm), so board_init() must arm its buttons as GPIO
 * wake sources and nothing in the board layer may poll while idle. Standby (deep sleep)
 * goes through board_standby_prepare().
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

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

typedef enum {
    BOARD_PRESS_SHORT,
    BOARD_PRESS_LONG,
} board_press_t;

/* Called from the board's button task, never from an ISR. Keep it short: post an event. */
typedef void (*board_button_cb_t)(board_button_t button, board_press_t press, void *ctx);

typedef struct {
    int clk_gpio;
    int data_gpio;
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
/* Live level of a button, for combinations. Only GPIO buttons can answer; the PWR
 * button reports false. */
bool board_button_is_down(board_button_t button);
/* `count` short buzzes (1 = recording, 2 = muted). Blocks for the duration. */
esp_err_t board_haptic_pulse(int count);

/* ---- microphone ---- */
board_mic_pins_t board_mic_pins(void);
/* EQ bands for this board's mic, measured on the device; *bands may be NULL (count 0). */
int board_mic_eq(const board_mic_eq_band_t **bands);

/* ---- storage (card powered only while mounted) ---- */
bool board_storage_card_present(void);
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
