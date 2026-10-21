#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "isotime.h"
#include "textfb.h"

static const char *TAG = "ui";

#define BAND_H 48       /* rows of one text line at scale 5 (35 px) plus margin */
/* A line is drawn in strips of this many rows, so the buffer stays small: 14 KB on the
 * T4-S3's 450-px panel instead of 43 KB, which internal RAM can't spare while it records
 * (four mics' DMA buffers, the session task and the Opus encoder). */
#define STRIP_H 16
#define BRIGHTNESS_PERCENT 60

/* Text sizes. Large panels (T-Watch Ultra, 410 px wide): 30 px glyph pitch at scale 5, 13
 * characters across. Small panels (T-Watch S3, 240x240): scale 3, 18 px pitch, 13 across,
 * and a compact layout with bands as tall as the text. */
static int s_text_scale = 5, s_small_scale = 3, s_message_scale = 6;
static bool s_compact;
#define TEXT_SCALE s_text_scale
#define SMALL_SCALE s_small_scale
#define MESSAGE_SCALE s_message_scale

static bool s_shown;
static board_display_info_t s_info;
static uint16_t *s_band; /* DISP width * STRIP_H, DMA-capable internal RAM */
/* No light sleep while the panel is on: it is a few seconds, and the panel's own draw
 * dwarfs what sleeping would save. */
static esp_pm_lock_handle_t s_pm_lock;

/* Rows one line of text at `scale` takes: a full band on large panels, the glyph plus a
 * small margin on compact ones. */
static int band_for(int scale)
{
    const int h = s_compact ? 7 * scale + 6 : BAND_H;
    return h < BAND_H ? h : BAND_H;
}

static esp_err_t push_line(int y, const char *text, int scale, uint16_t color)
{
    int h = band_for(scale);
    /* Small panels: keep the band on the panel. (Large panels draw as they always have.) */
    if (s_compact && (y < 0 || y >= s_info.height)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_compact && y + h > s_info.height) {
        h = s_info.height - y;
    }
    /* Shrink text that would not fit across rather than cutting it off (any panel: lines
     * that fit are drawn as before). */
    while (scale > 1 && textfb_text_width(text, scale) > s_info.width) {
        scale--;
    }
    const int w = textfb_text_width(text, scale);
    const int x = w < s_info.width ? (s_info.width - w) / 2 : 0;
    const int text_y = (h - 7 * scale) / 2;
    for (int o = 0; o < h; o += STRIP_H) {
        const int sh = h - o < STRIP_H ? h - o : STRIP_H;
        textfb_t fb = {s_band, s_info.width, sh};
        textfb_clear(&fb, 0);
        textfb_draw(&fb, x, text_y - o, text, scale, color); /* clipped to the strip */
        ESP_RETURN_ON_ERROR(board_display_draw(0, y + o, s_info.width, sh, s_band), TAG, "draw");
    }
    return ESP_OK;
}

static void pick_layout(void)
{
    s_compact = s_info.width < 400;
    s_text_scale = s_compact ? 3 : 5;
    s_small_scale = s_compact ? 2 : 3;
    s_message_scale = s_compact ? 4 : 6;
}

static esp_err_t clear_screen(void)
{
    textfb_t fb = {s_band, s_info.width, STRIP_H};
    textfb_clear(&fb, 0);
    for (int y = 0; y < s_info.height; y += STRIP_H) {
        const int h = (y + STRIP_H <= s_info.height) ? STRIP_H : s_info.height - y;
        ESP_RETURN_ON_ERROR(board_display_draw(0, y, s_info.width, h, s_band), TAG, "clear");
    }
    return ESP_OK;
}

typedef enum { VIEW_STATUS, VIEW_PAIRING, VIEW_MESSAGE, VIEW_RECORDING } view_t;
static view_t s_view; /* what is on screen while s_shown */

/* Power the panel on if needed; clear it when it was off or showed another view. */
static esp_err_t screen_begin(view_t view)
{
    const bool clear = !s_shown || s_view != view;
    if (!s_shown) {
        if (!s_pm_lock) {
            esp_pm_lock_create(ESP_PM_NO_LIGHT_SLEEP, 0, "ui", &s_pm_lock);
        }
        ESP_RETURN_ON_ERROR(board_display_on(&s_info), TAG, "display on");
        pick_layout();
        if (!s_band) {
            s_band = heap_caps_malloc((size_t)s_info.width * STRIP_H * sizeof(uint16_t),
                                      MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
            ESP_RETURN_ON_FALSE(s_band, ESP_ERR_NO_MEM, TAG, "band buffer");
        }
        if (s_pm_lock) {
            esp_pm_lock_acquire(s_pm_lock);
        }
        s_shown = true;
    }
    s_view = view;
    return clear ? clear_screen() : ESP_OK;
}

esp_err_t ui_show_status(const ui_status_t *s)
{
    ESP_RETURN_ON_ERROR(screen_begin(VIEW_STATUS), TAG, "screen");

    const uint16_t white = textfb_rgb(255, 255, 255);
    const uint16_t grey = textfb_rgb(140, 140, 140);
    const uint16_t green = textfb_rgb(40, 220, 60);
    const uint16_t red = textfb_rgb(240, 40, 40);
    const uint16_t amber = textfb_rgb(250, 180, 30);
    char line[32];
    /* Where each line starts: the first row, then the step after each of the 8 lines. */
    static const int steps_large[] = {70, 60, 70, 60, 70, 80, 36};
    static const int steps_compact[] = {30, 27, 30, 27, 30, 30, 22};
    const int *step = s_compact ? steps_compact : steps_large;
    int y = s_compact ? 8 : 40;

    if (s->recording_file) {
        snprintf(line, sizeof(line), "WAV %d/%dS", s->rec_elapsed_s, s->rec_total_s);
        push_line(y, line, TEXT_SCALE, amber);
    } else if (s->session) {
        snprintf(line, sizeof(line), "%s %d:%02d", s->note ? "NOTE" : "REC", s->session_elapsed_s / 60,
                 s->session_elapsed_s % 60);
        push_line(y, line, TEXT_SCALE, green);
    } else if (s->muted) {
        push_line(y, "MUTED", TEXT_SCALE, red);
    } else {
        push_line(y, "READY", TEXT_SCALE, white);
    }
    y += step[0];

    if (!s->battery.present) {
        snprintf(line, sizeof(line), "NO BATTERY");
    } else if (s->battery_low) {
        snprintf(line, sizeof(line), "BAT %d%% LOW", s->battery.percent);
    } else if (s->battery.charging) {
        snprintf(line, sizeof(line), "BAT %d%% CHG", s->battery.percent);
    } else {
        snprintf(line, sizeof(line), "BAT %d%%", s->battery.percent);
    }
    push_line(y, line, TEXT_SCALE, s->battery_low ? red : white);
    y += step[1];
    snprintf(line, sizeof(line), "%d.%02dV%s", s->battery.millivolts / 1000,
             (s->battery.millivolts % 1000) / 10, s->battery.vbus ? " USB" : "");
    push_line(y, line, TEXT_SCALE, grey);
    y += step[2];

    if (s->time_valid) {
        isotime_fields_t f;
        isotime_to_fields(s->now, &f);
        snprintf(line, sizeof(line), "%02d:%02d:%02d", f.hour, f.minute, f.second);
        push_line(y, line, TEXT_SCALE, white);
        y += step[3];
        snprintf(line, sizeof(line), "%04d-%02d-%02d", f.year, f.month, f.day);
        push_line(y, line, TEXT_SCALE, grey);
    } else {
        push_line(y, "TIME NOT SET", TEXT_SCALE, amber);
        y += step[3];
        push_line(y, "USE: TIME ISO", SMALL_SCALE, grey);
    }
    y += step[4];

    const char *store = board_storage_label();
    const bool card = strcmp(store, "SD") == 0;
    if (s->sd_mounted && s->sd_free_mb >= 1024) {
        snprintf(line, sizeof(line), "%s %llu.%lluGB FREE", store,
                 (unsigned long long)(s->sd_free_mb / 1024),
                 (unsigned long long)((s->sd_free_mb % 1024) * 10 / 1024));
    } else if (s->sd_mounted) {
        snprintf(line, sizeof(line), "%s %lluMB FREE", store, (unsigned long long)s->sd_free_mb);
    } else if (card) {
        snprintf(line, sizeof(line), "%s", s->sd_present ? "SD CARD IN" : "SD NO CARD");
    } else {
        snprintf(line, sizeof(line), "%s %s", store, s->sd_present ? "READY" : "MISSING");
    }
    push_line(y, line, TEXT_SCALE, s->sd_present || s->sd_mounted ? white : amber);
    y += step[5];

    if (s->ble_connected) {
        snprintf(line, sizeof(line), "PHONE: SYNCING %d", s->queued_chunks);
    } else if (s->queued_chunks) {
        snprintf(line, sizeof(line), "QUEUE %d (%d MIN)", s->queued_chunks, s->queued_min);
    } else {
        snprintf(line, sizeof(line), "QUEUE EMPTY");
    }
    push_line(y, line, SMALL_SCALE, s->queued_chunks ? amber : grey);
    y += step[6];
    push_line(y, board_button_hint(), SMALL_SCALE, grey);
    /* The init sequence leaves the panel at brightness 0; raise it once the frame is in. */
    return board_display_set_brightness(BRIGHTNESS_PERCENT);
}

esp_err_t ui_show_passkey(uint32_t passkey)
{
    ESP_RETURN_ON_ERROR(screen_begin(VIEW_PAIRING), TAG, "screen");
    const uint16_t white = textfb_rgb(255, 255, 255);
    const uint16_t grey = textfb_rgb(140, 140, 140);
    char line[16];
    /* Rows 120, 220 and 330 of the Ultra's 502, scaled to the panel. */
    push_line(s_info.height * 120 / 502, "PAIRING CODE", TEXT_SCALE, grey);
    snprintf(line, sizeof(line), "%03u %03u", (unsigned)(passkey / 1000), (unsigned)(passkey % 1000));
    push_line(s_info.height * 220 / 502, line, TEXT_SCALE, white);
    push_line(s_info.height * 330 / 502, "TYPE IT ON THE PHONE", SMALL_SCALE, grey);
    return board_display_set_brightness(BRIGHTNESS_PERCENT);
}

bool ui_is_pairing(void)
{
    return s_shown && s_view == VIEW_PAIRING;
}

esp_err_t ui_show_message(const char *big, const char *small)
{
    ESP_RETURN_ON_ERROR(screen_begin(VIEW_MESSAGE), TAG, "screen");
    const uint16_t white = textfb_rgb(255, 255, 255);
    const uint16_t grey = textfb_rgb(140, 140, 140);
    /* As large as fits across (a line is one band, so 6 is the most: 42 px of 48). */
    int scale = MESSAGE_SCALE;
    while (scale > TEXT_SCALE && textfb_text_width(big, scale) > s_info.width) {
        scale--;
    }
    const int y = s_info.height / 2 - band_for(scale);
    push_line(y, big, scale, white);
    push_line(y + band_for(scale) + (s_compact ? 6 : 12), small ? small : "", SMALL_SCALE, grey);
    return board_display_set_brightness(BRIGHTNESS_PERCENT);
}

#define REC_BRIGHTNESS_PERCENT 40 /* on for hours: dimmer, less burn-in */
#define REC_SHIFT_STEPS 5         /* line positions it cycles through, one per minute */
#define REC_SHIFT_PX 8

esp_err_t ui_show_recording(const ui_rec_t *r)
{
    /* A new minute moves every line; clear so nothing is left behind. */
    static int s_rec_shift = -1;
    const int shift = (int)((r->elapsed_s / 60) % REC_SHIFT_STEPS) * REC_SHIFT_PX -
                      (REC_SHIFT_STEPS / 2) * REC_SHIFT_PX;
    const bool moved = s_shown && s_view == VIEW_RECORDING && shift != s_rec_shift;
    ESP_RETURN_ON_ERROR(screen_begin(VIEW_RECORDING), TAG, "screen");
    if (moved) {
        ESP_RETURN_ON_ERROR(clear_screen(), TAG, "clear");
    }
    s_rec_shift = shift;

    const uint16_t white = textfb_rgb(255, 255, 255);
    const uint16_t grey = textfb_rgb(140, 140, 140);
    const uint16_t red = textfb_rgb(240, 40, 40);
    const uint16_t amber = textfb_rgb(255, 176, 0);
    const int big = band_for(MESSAGE_SCALE);
    const int gap = s_compact ? 6 : 16;
    int y = s_info.height / 4 + shift;
    char line[32];

    push_line(y, r->note ? "NOTE" : "REC", MESSAGE_SCALE, red);
    y += big + gap;
    const uint32_t h = r->elapsed_s / 3600, m = (r->elapsed_s / 60) % 60, sec = r->elapsed_s % 60;
    if (h) {
        snprintf(line, sizeof(line), "%u:%02u:%02u", (unsigned)h, (unsigned)m, (unsigned)sec);
    } else {
        snprintf(line, sizeof(line), "%u:%02u", (unsigned)m, (unsigned)sec);
    }
    push_line(y, line, MESSAGE_SCALE, white);
    y += big + 3 * gap;

    if (r->mics_mixed >= 0) {
        if (r->mics_mixed == 0) {
            snprintf(line, sizeof(line), "NO MICS");
        } else {
            snprintf(line, sizeof(line), "%d MIC%s", r->mics_mixed, r->mics_mixed == 1 ? "" : "S");
        }
        push_line(y, line, TEXT_SCALE, r->mics_mixed ? grey : amber);
        y += band_for(TEXT_SCALE) + gap;
    }
    if (r->battery_known && r->battery.vbus) {
        if (r->battery.present && r->battery.percent >= 0) {
            snprintf(line, sizeof(line), "USB  BATT %d%%", r->battery.percent);
        } else {
            snprintf(line, sizeof(line), "USB POWER");
        }
    } else if (r->battery_known && r->battery.percent >= 0) {
        snprintf(line, sizeof(line), "BATTERY %d%%", r->battery.percent);
    } else {
        snprintf(line, sizeof(line), "BATTERY ?");
    }
    push_line(y, line, SMALL_SCALE, r->battery_low ? amber : grey);
    y += band_for(SMALL_SCALE) + gap;
    push_line(y, r->stop_hint ? r->stop_hint : "", SMALL_SCALE, grey);
    return board_display_set_brightness(REC_BRIGHTNESS_PERCENT);
}

esp_err_t ui_hide(void)
{
    if (!s_shown) {
        return ESP_OK;
    }
    s_shown = false;
    if (s_pm_lock) {
        esp_pm_lock_release(s_pm_lock);
    }
    return board_display_off();
}

bool ui_is_shown(void)
{
    return s_shown;
}
