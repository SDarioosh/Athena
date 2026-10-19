/* Status screen: a few text lines on black, drawn band by band through board_display_*.
 * No LVGL: the screen is on for ~5 s per button press and shows six lines. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "board.h"
#include "esp_err.h"

typedef struct {
    bool muted;
    bool recording_file; /* the test recorder is writing a WAV */
    int rec_elapsed_s;
    int rec_total_s;
    bool session;        /* an Opus session is recording */
    int session_elapsed_s;
    int queued_chunks;   /* waiting for the phone */
    int queued_min;
    bool ble_connected;
    board_battery_t battery;
    int64_t now;      /* UTC epoch seconds */
    bool time_valid;
    bool sd_present;  /* card-detect */
    bool sd_mounted;
    uint64_t sd_free_mb; /* valid when mounted */
} ui_status_t;

/* Power the display on (if off) and draw the status. Safe to call repeatedly to refresh. */
esp_err_t ui_show_status(const ui_status_t *s);
/* The BLE pairing code (§3.1 of docs/twatch-ble-protocol.md), large, until ui_hide(). */
esp_err_t ui_show_passkey(uint32_t passkey);
bool ui_is_pairing(void);
esp_err_t ui_hide(void);
bool ui_is_shown(void);
