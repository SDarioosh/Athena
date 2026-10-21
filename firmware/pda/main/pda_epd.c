#include "pda_epd.h"

#include <string.h>

#include "epd_highlevel.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static const char *TAG = "epd";

static EpdiyHighlevelState s_hl;
static SemaphoreHandle_t s_lock;
static esp_pm_lock_handle_t s_pm;
static pda_epd_stats_t s_stats;
static bool s_ready;
static int s_rot_deg = PDA_ROT_DEFAULT;

esp_err_t pda_epd_init(i2c_master_bus_handle_t bus)
{
    const EpdI2cConfig i2c = {.bus_handle = bus};
    const EpdInitConfig cfg = {.i2c = &i2c};
    epd_init_with_config(&epd_board_v7, &ED047TC1, EPD_LUT_64K, &cfg);
    epd_set_vcom(PDA_EPD_VCOM_MV);
    nvs_handle_t h;
    if (nvs_open("pda", NVS_READONLY, &h) == ESP_OK) {
        int16_t r = 0;
        if (nvs_get_i16(h, "rot", &r) == ESP_OK && (r == 90 || r == 270)) {
            s_rot_deg = r;
        }
        nvs_close(h);
    }
    epd_set_rotation(s_rot_deg == 90 ? EPD_ROT_PORTRAIT : EPD_ROT_INVERTED_PORTRAIT);
    s_hl = epd_hl_init(EPD_BUILTIN_WAVEFORM);
    s_lock = xSemaphoreCreateMutex();
    /* The waveform lookup runs on the CPU while the LCD peripheral streams lines: keep the
     * clock up during a refresh (DFS would otherwise drop it). */
    esp_err_t err = esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "epd", &s_pm);
    if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED) {
        return err;
    }
    s_ready = true;
    /* No clear here (it was epd_fullclear: three black/white cycles, ~1.9 s, before the UI's
     * own GC16 redraw). The panel keeps its image, the Asleep screen after a wake, until
     * the caller's first update: PDA_EPD_CLEAN, one cycle and every pixel drawn from white.
     * epd_hl_init left both framebuffers white, which is what that update assumes. A caller
     * that wants partial updates over the image on the panel redraws that image and calls
     * pda_epd_mark_shown() first (a timer wake's "synced" line). */
    ESP_LOGI(TAG, "ready: %dx%d, portrait %d, VCOM %d mV", epd_width(), epd_height(), s_rot_deg,
             PDA_EPD_VCOM_MV);
    return ESP_OK;
}

void pda_epd_set_rotation(int degrees)
{
    if (degrees != 90 && degrees != 270) {
        return;
    }
    s_rot_deg = degrees;
    epd_set_rotation(degrees == 90 ? EPD_ROT_PORTRAIT : EPD_ROT_INVERTED_PORTRAIT);
    nvs_handle_t h;
    if (nvs_open("pda", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_i16(h, "rot", (int16_t)degrees);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGI(TAG, "rotation: portrait %d", degrees);
}

int pda_epd_rotation(void)
{
    return s_rot_deg;
}

/* The inverse of epdiy's _rotate() for the two portrait modes. */
void pda_epd_touch_to_screen(int px, int py, int *x, int *y)
{
    if (s_rot_deg == 90) { /* EPD_ROT_PORTRAIT: phys x = 959 - y, phys y = x */
        *x = py;
        *y = PDA_EPD_W - 1 - px;
    } else { /* EPD_ROT_INVERTED_PORTRAIT: phys x = y, phys y = 539 - x */
        *x = PDA_EPD_H - 1 - py;
        *y = px;
    }
}

uint8_t *pda_epd_fb(void)
{
    return epd_hl_get_framebuffer(&s_hl);
}

void pda_epd_clear_fb(void)
{
    epd_hl_set_all_white(&s_hl);
}

void pda_epd_mark_shown(void)
{
    if (!s_ready) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    memcpy(s_hl.back_fb, s_hl.front_fb, PDA_EPD_W / 2 * PDA_EPD_H);
    xSemaphoreGive(s_lock);
}

static pda_epd_kind_t run_update(pda_epd_kind_t kind, const EpdRect *area, bool count)
{
    if (!s_ready) {
        return kind;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (count && kind != PDA_EPD_CLEAN && s_stats.since_clean >= PDA_EPD_PARTIAL_LIMIT) {
        kind = PDA_EPD_CLEAN; /* ghosting budget used up */
        area = NULL;
    }
    if (s_pm) {
        esp_pm_lock_acquire(s_pm);
    }
    const int64_t t0 = esp_timer_get_time();
    epd_poweron();
    const int temp = (int)epd_ambient_temperature();
    enum EpdDrawError e;
    if (kind == PDA_EPD_CLEAN) {
        /* epdiy's highlevel update only drives pixels that differ from the back buffer, so
         * unchanged (ghosted) areas are never touched otherwise. Flash the panel white (one
         * black/white cycle; epd_clear()'s default takes ~1.9 s), tell epdiy so, and draw
         * everything from white. */
        epd_clear_area_cycles(epd_full_screen(), 1, 50);
        memset(s_hl.back_fb, 0xFF, PDA_EPD_W / 2 * PDA_EPD_H);
        e = epd_hl_update_screen(&s_hl, MODE_GC16, temp);
    } else if (kind == PDA_EPD_FULL) {
        e = epd_hl_update_screen(&s_hl, MODE_GC16, temp);
    } else if (area) {
        e = epd_hl_update_area(&s_hl, MODE_GL16, temp, *area);
    } else {
        e = epd_hl_update_screen(&s_hl, MODE_GL16, temp);
    }
    epd_poweroff();
    const int ms = (int)((esp_timer_get_time() - t0) / 1000);
    if (s_pm) {
        esp_pm_lock_release(s_pm);
    }
    s_stats.count[kind]++;
    s_stats.last_ms[kind] = ms;
    if (kind == PDA_EPD_CLEAN) {
        s_stats.since_clean = 0;
    } else if (count) {
        s_stats.since_clean++;
    }
    xSemaphoreGive(s_lock);
    if (e != EPD_DRAW_SUCCESS) {
        ESP_LOGW(TAG, "update error 0x%x", (unsigned)e);
    }
    ESP_LOGD(TAG, "update kind %d: %d ms", (int)kind, ms);
    return kind;
}

pda_epd_kind_t pda_epd_update(pda_epd_kind_t kind)
{
    return run_update(kind, NULL, true);
}

pda_epd_kind_t pda_epd_update_area(EpdRect area)
{
    return run_update(PDA_EPD_PARTIAL, &area, true);
}

void pda_epd_update_light(EpdRect area)
{
    run_update(PDA_EPD_PARTIAL, &area, false);
}

void pda_epd_get_stats(pda_epd_stats_t *out)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    *out = s_stats;
    xSemaphoreGive(s_lock);
}
