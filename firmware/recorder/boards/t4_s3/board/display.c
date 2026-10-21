#include "display.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pins.h"

static const char *TAG = "display";
static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static bool s_bus_up;

#define DISP_SPI_HOST SPI2_HOST
#define DISP_MAX_TRANSFER (DISP_WIDTH * 64 * 2)

/* Panel init sequence for the 2.41" 450x600 RM690B0 of the T4-S3.
 * From LilyGo-AMOLED-Series src/initSequence.cpp (rm690b0_cmd), MIT License,
 * Copyright (c) 2023 Shenzhen Xin Yuan Electronic Technology Co., Ltd.
 * COLMOD (0x3A) is omitted: the esp_lcd_co5300 driver sends it from bits_per_pixel (0x55,
 * 16 bits per pixel, the value LilyGO sends). Brightness comes later (display_set_brightness),
 * after the first frame, so the panel lights up with a drawn image. */
static const co5300_lcd_init_cmd_t s_init_cmds[] = {
    /* {cmd, data, data_bytes, delay_ms} */
    {0xFE, (uint8_t[]){0x20}, 1, 0},  /* command page 0x20 */
    {0x26, (uint8_t[]){0x0A}, 1, 0},  /* MIPI off */
    {0x24, (uint8_t[]){0x80}, 1, 0},  /* RAM written over SPI */
    {0x5A, (uint8_t[]){0x51}, 1, 0},  /* SWIRE for the BV6804 power IC */
    {0x5B, (uint8_t[]){0x2E}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},  /* user command page */
    {0xC2, (uint8_t[]){0x00}, 1, 10},
    {0x35, (uint8_t[]){0x00}, 1, 0},  /* TE on (the line is unused) */
    {0x51, (uint8_t[]){0x00}, 1, 0},  /* brightness 0 */
    {0x11, (uint8_t[]){0x00}, 0, 120}, /* sleep out */
    {0x29, (uint8_t[]){0x00}, 0, 10},  /* display on */
};

esp_err_t display_init(void)
{
    if (s_panel) {
        return ESP_OK;
    }
    const spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(PIN_DISP_SCK, PIN_DISP_D0, PIN_DISP_D1,
                                                             PIN_DISP_D2, PIN_DISP_D3,
                                                             DISP_MAX_TRANSFER);
    ESP_RETURN_ON_ERROR(spi_bus_initialize(DISP_SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");
    s_bus_up = true;

    esp_lcd_panel_io_spi_config_t io = CO5300_PANEL_IO_QSPI_CONFIG(PIN_DISP_CS, NULL, NULL);
    /* LilyGO runs this panel at 36 MHz. */
    io.pclk_hz = 36 * 1000 * 1000;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)DISP_SPI_HOST, &io, &s_io),
                        TAG, "panel io");

    const co5300_vendor_config_t vendor = {
        .init_cmds = s_init_cmds,
        .init_cmds_size = sizeof(s_init_cmds) / sizeof(s_init_cmds[0]),
        .flags.use_qspi_interface = 1,
    };
    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = PIN_DISP_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = (void *)&vendor,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_co5300(s_io, &panel_cfg, &s_panel), TAG, "panel");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_set_gap(s_panel, DISP_X_OFFSET, DISP_Y_OFFSET), TAG, "gap");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "on");
    return ESP_OK;
}

esp_err_t display_draw(int x, int y, int w, int h, const uint16_t *pixels)
{
    ESP_RETURN_ON_FALSE(s_panel, ESP_ERR_INVALID_STATE, TAG, "off");
    /* esp_lcd's end coordinates are exclusive. */
    return esp_lcd_panel_draw_bitmap(s_panel, x, y, x + w, y + h, pixels);
}

esp_err_t display_set_brightness(int percent)
{
    ESP_RETURN_ON_FALSE(s_panel, ESP_ERR_INVALID_STATE, TAG, "off");
    return esp_lcd_panel_co5300_set_brightness(s_panel, (uint8_t)percent);
}

esp_err_t display_deinit(void)
{
    if (s_panel) {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_del(s_panel);
        s_panel = NULL;
    }
    if (s_io) {
        esp_lcd_panel_io_del(s_io);
        s_io = NULL;
    }
    if (s_bus_up) {
        spi_bus_free(DISP_SPI_HOST);
        s_bus_up = false;
    }
    /* Leave every panel line Hi-Z so nothing back-powers the panel through its inputs
     * once its power is off. */
    const int pins[] = {PIN_DISP_CS, PIN_DISP_SCK, PIN_DISP_D0, PIN_DISP_D1, PIN_DISP_D2,
                        PIN_DISP_D3, PIN_DISP_RST};
    for (unsigned i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        gpio_reset_pin(pins[i]);
        gpio_set_direction(pins[i], GPIO_MODE_INPUT);
        gpio_set_pull_mode(pins[i], GPIO_FLOATING);
    }
    return ESP_OK;
}
