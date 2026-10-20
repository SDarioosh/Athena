/* odysseus-sound PDA on the LilyGO T5 E-Paper Pro (docs/t5-pda-plan.md). Bring-up build. */
#include <stdio.h>

#include "esp_log.h"
#include "fonts.h"
#include "nvs_flash.h"
#include "pda_console.h"
#include "pda_epd.h"
#include "t5_board.h"
#include "touch_gt911.h"

static const char *TAG = "app";

static void on_button(t5_button_t b, t5_press_t p, void *ctx)
{
    (void)ctx;
    ESP_LOGI(TAG, "button %s %s", b == T5_BUTTON_BOOT ? "boot" : "side",
             p == T5_PRESS_SHORT ? "short" : "long");
}

static void on_touch(const gesture_t *g, void *ctx)
{
    (void)ctx;
    (void)g;
}

void pda_draw_test_screen(void)
{
    uint8_t *fb = pda_epd_fb();
    pda_epd_clear_fb();
    int x = 40, y = 80;
    epd_write_default(&FONT_BIG, "odysseus-sound PDA", &x, &y, fb);
    x = 40;
    y = 140;
    epd_write_default(&FONT_TEXT, "Bring-up: Latin-1 äöü ß é ñ — “quotes” … • ½ °C", &x, &y, fb);
    for (int i = 0; i < 16; i++) {
        epd_fill_rect((EpdRect){.x = 40 + i * 55, .y = 200, .width = 50, .height = 80},
                      (uint8_t)(i * 17), fb);
    }
    epd_draw_rect((EpdRect){.x = 0, .y = 0, .width = PDA_EPD_W, .height = PDA_EPD_H}, 0, fb);
    /* Touch targets for the coordinate check: 1 top left .. 4 bottom left, clockwise. */
    static const int cx[4] = {60, PDA_EPD_W - 60, PDA_EPD_W - 60, 60};
    static const int cy[4] = {320, 320, PDA_EPD_H - 40, PDA_EPD_H - 40};
    for (int i = 0; i < 4; i++) {
        epd_fill_circle(cx[i], cy[i], 22, EPD_BLACK, fb);
        char n[2] = {(char)('1' + i), 0};
        int tx = cx[i] - 7, ty = cy[i] + 10;
        EpdFontProperties p = epd_font_properties_default();
        p.fg_color = 0xF;
        p.bg_color = 0x0;
        epd_write_string(&FONT_TEXT, n, &tx, &ty, fb, &p);
    }
    pda_epd_update(PDA_EPD_FULL);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    ESP_ERROR_CHECK(t5_board_init());
    ESP_ERROR_CHECK(pda_epd_init(t5_board_i2c()));
    if (touch_init(t5_board_i2c(), on_touch, NULL) != ESP_OK) {
        ESP_LOGE(TAG, "touch unavailable");
    }
    t5_board_start_buttons(on_button, NULL);
    pda_draw_test_screen();
    pda_console_start();
}

void pda_console_register_app(void) {}
