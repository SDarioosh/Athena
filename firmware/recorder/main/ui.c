#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "isotime.h"
#include "textfb.h"

static const char *TAG = "ui";

#define BAND_H 48       /* rows per push: one text line at scale 5 (35 px) plus margin */
#define TEXT_SCALE 5    /* 30 px glyph pitch: 13 characters across 410 px */
#define SMALL_SCALE 3
#define BRIGHTNESS_PERCENT 60

static bool s_shown;
static board_display_info_t s_info;
static uint16_t *s_band; /* DISP width * BAND_H, DMA-capable internal RAM */

static esp_err_t push_line(int y, const char *text, int scale, uint16_t color)
{
    textfb_t fb = {s_band, s_info.width, BAND_H};
    textfb_clear(&fb, 0);
    const int w = textfb_text_width(text, scale);
    const int x = w < s_info.width ? (s_info.width - w) / 2 : 0;
    textfb_draw(&fb, x, (BAND_H - 7 * scale) / 2, text, scale, color);
    return board_display_draw(0, y, s_info.width, BAND_H, s_band);
}

static esp_err_t clear_screen(void)
{
    textfb_t fb = {s_band, s_info.width, BAND_H};
    textfb_clear(&fb, 0);
    for (int y = 0; y < s_info.height; y += BAND_H) {
        const int h = (y + BAND_H <= s_info.height) ? BAND_H : s_info.height - y;
        ESP_RETURN_ON_ERROR(board_display_draw(0, y, s_info.width, h, s_band), TAG, "clear");
    }
    return ESP_OK;
}

esp_err_t ui_show_status(const ui_status_t *s)
{
    if (!s_shown) {
        ESP_RETURN_ON_ERROR(board_display_on(&s_info), TAG, "display on");
        if (!s_band) {
            s_band = heap_caps_malloc((size_t)s_info.width * BAND_H * sizeof(uint16_t),
                                      MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
            ESP_RETURN_ON_FALSE(s_band, ESP_ERR_NO_MEM, TAG, "band buffer");
        }
        s_shown = true;
        ESP_RETURN_ON_ERROR(clear_screen(), TAG, "clear");
    }

    const uint16_t white = textfb_rgb(255, 255, 255);
    const uint16_t grey = textfb_rgb(140, 140, 140);
    const uint16_t green = textfb_rgb(40, 220, 60);
    const uint16_t red = textfb_rgb(240, 40, 40);
    const uint16_t amber = textfb_rgb(250, 180, 30);
    char line[32];
    int y = 40;

    if (s->recording_file) {
        snprintf(line, sizeof(line), "WAV %d/%dS", s->rec_elapsed_s, s->rec_total_s);
        push_line(y, line, TEXT_SCALE, amber);
    } else if (s->session) {
        snprintf(line, sizeof(line), "REC %d:%02d", s->session_elapsed_s / 60,
                 s->session_elapsed_s % 60);
        push_line(y, line, TEXT_SCALE, green);
    } else if (s->muted) {
        push_line(y, "MUTED", TEXT_SCALE, red);
    } else {
        push_line(y, "READY", TEXT_SCALE, white);
    }
    y += 70;

    if (!s->battery.present) {
        snprintf(line, sizeof(line), "NO BATTERY");
    } else if (s->battery.charging) {
        snprintf(line, sizeof(line), "BAT %d%% CHG", s->battery.percent);
    } else {
        snprintf(line, sizeof(line), "BAT %d%%", s->battery.percent);
    }
    push_line(y, line, TEXT_SCALE, white);
    y += 60;
    snprintf(line, sizeof(line), "%d.%02dV%s", s->battery.millivolts / 1000,
             (s->battery.millivolts % 1000) / 10, s->battery.vbus ? " USB" : "");
    push_line(y, line, TEXT_SCALE, grey);
    y += 70;

    if (s->time_valid) {
        isotime_fields_t f;
        isotime_to_fields(s->now, &f);
        snprintf(line, sizeof(line), "%02d:%02d:%02d", f.hour, f.minute, f.second);
        push_line(y, line, TEXT_SCALE, white);
        y += 60;
        snprintf(line, sizeof(line), "%04d-%02d-%02d", f.year, f.month, f.day);
        push_line(y, line, TEXT_SCALE, grey);
    } else {
        push_line(y, "TIME NOT SET", TEXT_SCALE, amber);
        y += 60;
        push_line(y, "USE: TIME ISO", SMALL_SCALE, grey);
    }
    y += 70;

    if (s->sd_mounted) {
        snprintf(line, sizeof(line), "SD %llu.%lluGB FREE", (unsigned long long)(s->sd_free_mb / 1024),
                 (unsigned long long)((s->sd_free_mb % 1024) * 10 / 1024));
    } else if (s->sd_present) {
        snprintf(line, sizeof(line), "SD CARD IN");
    } else {
        snprintf(line, sizeof(line), "SD NO CARD");
    }
    push_line(y, line, TEXT_SCALE, s->sd_present || s->sd_mounted ? white : amber);
    y += 80;

    if (s->ble_connected) {
        snprintf(line, sizeof(line), "PHONE: SYNCING %d", s->queued_chunks);
    } else if (s->queued_chunks) {
        snprintf(line, sizeof(line), "QUEUE %d (%d MIN)", s->queued_chunks, s->queued_min);
    } else {
        snprintf(line, sizeof(line), "QUEUE EMPTY");
    }
    push_line(y, line, SMALL_SCALE, s->queued_chunks ? amber : grey);
    y += 36;
    push_line(y, "BOOT+PWR: REC  BOOT: MUTE", SMALL_SCALE, grey);
    /* The init sequence leaves the panel at brightness 0; raise it once the frame is in. */
    return board_display_set_brightness(BRIGHTNESS_PERCENT);
}

esp_err_t ui_hide(void)
{
    if (!s_shown) {
        return ESP_OK;
    }
    s_shown = false;
    return board_display_off();
}

bool ui_is_shown(void)
{
    return s_shown;
}
