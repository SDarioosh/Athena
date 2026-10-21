#include "display.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "pins.h"

static const char *TAG = "display";
static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static bool s_bus_up;
static SemaphoreHandle_t s_done;
static StaticSemaphore_t s_done_buf;

#define DISP_SPI_HOST SPI2_HOST
#define DISP_SPI_HZ (40 * 1000 * 1000)          /* as LilyGO's TFT_eSPI setup */
#define DISP_MAX_TRANSFER (DISP_WIDTH * 48 * 2) /* one UI band */

static bool on_color_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *edata,
                          void *ctx)
{
    (void)io;
    (void)edata;
    (void)ctx;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    return woken == pdTRUE;
}

/* Hi-Z, so nothing back-powers the panel once its AVDD rail is off. */
static void release_pins(void)
{
    const int pins[] = {PIN_DISP_MOSI, PIN_DISP_SCK, PIN_DISP_DC, PIN_DISP_CS};
    for (unsigned i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        gpio_reset_pin(pins[i]);
        gpio_set_direction(pins[i], GPIO_MODE_INPUT);
        gpio_set_pull_mode(pins[i], GPIO_FLOATING);
    }
}

esp_err_t display_init(void)
{
    if (s_panel) {
        return ESP_OK;
    }
    if (!s_done) {
        s_done = xSemaphoreCreateBinaryStatic(&s_done_buf);
    }
    /* Backlight off until the first frame is in (board_display_set_brightness()). */
    gpio_reset_pin(PIN_DISP_BL);
    gpio_set_direction(PIN_DISP_BL, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_DISP_BL, 0);

    const spi_bus_config_t bus = {
        .mosi_io_num = PIN_DISP_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = PIN_DISP_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = DISP_MAX_TRANSFER,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(DISP_SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");
    s_bus_up = true;

    const esp_lcd_panel_io_spi_config_t io = {
        .cs_gpio_num = PIN_DISP_CS,
        .dc_gpio_num = PIN_DISP_DC,
        .spi_mode = 0,
        .pclk_hz = DISP_SPI_HZ,
        .trans_queue_depth = 4,
        .on_color_trans_done = on_color_done,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    esp_err_t err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)DISP_SPI_HOST, &io, &s_io);
    if (err == ESP_OK) {
        const esp_lcd_panel_dev_config_t cfg = {
            .reset_gpio_num = -1, /* no reset line: software reset */
            .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
            .bits_per_pixel = 16,
        };
        err = esp_lcd_new_panel_st7789(s_io, &cfg, &s_panel);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_reset(s_panel);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_init(s_panel);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_invert_color(s_panel, true);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_mirror(s_panel, true, true);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_set_gap(s_panel, 0, DISP_Y_OFFSET);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_disp_on_off(s_panel, true);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "panel init failed: %s", esp_err_to_name(err));
        display_deinit();
    }
    return err;
}

esp_err_t display_draw(int x, int y, int w, int h, const uint16_t *pixels)
{
    ESP_RETURN_ON_FALSE(s_panel, ESP_ERR_INVALID_STATE, TAG, "off");
    xSemaphoreTake(s_done, 0); /* drop a stale give */
    /* esp_lcd's end coordinates are exclusive. The transfer is queued: wait for it. */
    ESP_RETURN_ON_ERROR(esp_lcd_panel_draw_bitmap(s_panel, x, y, x + w, y + h, pixels), TAG, "draw");
    ESP_RETURN_ON_FALSE(xSemaphoreTake(s_done, pdMS_TO_TICKS(500)) == pdTRUE, ESP_ERR_TIMEOUT, TAG,
                        "draw timeout");
    return ESP_OK;
}

esp_err_t display_set_brightness(int percent)
{
    ESP_RETURN_ON_FALSE(s_panel, ESP_ERR_INVALID_STATE, TAG, "off");
    return gpio_set_level(PIN_DISP_BL, percent > 0 ? 1 : 0);
}

esp_err_t display_deinit(void)
{
    /* Backlight off, then GPIO45 back to an input (its 10k pull-down holds the strap level). */
    gpio_set_level(PIN_DISP_BL, 0);
    gpio_reset_pin(PIN_DISP_BL);
    gpio_set_direction(PIN_DISP_BL, GPIO_MODE_INPUT);
    gpio_set_pull_mode(PIN_DISP_BL, GPIO_FLOATING);
    if (s_panel) {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_disp_sleep(s_panel, true);
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
    release_pins();
    return ESP_OK;
}
