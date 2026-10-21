#include "touch_gt911.h"

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pda_epd.h"
#include "t5_pins.h"

static const char *TAG = "touch";

#define POLL_MS 25
#define TASK_STACK 3072
#define REG_PRODUCT_ID 0x8140
#define REG_STATUS 0x814E
#define REG_POINT1 0x814F
#define REG_CONFIG_RES 0x8048 /* x max u16 LE, y max u16 LE */

/* GT911 frame -> upright landscape panel, checked on the board (taps on corner targets);
 * pda_epd_touch_to_screen() then turns it into the portrait screen. */
#define TOUCH_SWAP_XY 1
#define TOUCH_FLIP_X 0
#define TOUCH_FLIP_Y 1

static i2c_master_dev_handle_t s_dev;
static touch_cb_t s_cb;
static void *s_ctx;
static int s_xmax = PDA_EPD_W, s_ymax = PDA_EPD_H;
static char s_id[5];
static volatile int64_t s_trace_until;
static uint32_t s_reads, s_errors, s_gestures;
static volatile bool s_asleep;

static esp_err_t rd(uint16_t reg, uint8_t *buf, size_t len)
{
    const uint8_t r[2] = {(uint8_t)(reg >> 8), (uint8_t)reg};
    return i2c_master_transmit_receive(s_dev, r, 2, buf, len, 50);
}

static esp_err_t wr8(uint16_t reg, uint8_t v)
{
    const uint8_t b[3] = {(uint8_t)(reg >> 8), (uint8_t)reg, v};
    return i2c_master_transmit(s_dev, b, 3, 50);
}

/* GT911 datasheet reset timing: INT low while RST rises selects address 0x5D. */
static void hw_reset(void)
{
    gpio_config_t out = {
        .pin_bit_mask = (1ULL << T5_TOUCH_RST) | (1ULL << T5_TOUCH_INT),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&out);
    gpio_set_level(T5_TOUCH_INT, 0);
    gpio_set_level(T5_TOUCH_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(11));
    gpio_set_level(T5_TOUCH_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(6));
    gpio_set_direction(T5_TOUCH_INT, GPIO_MODE_INPUT);
    vTaskDelay(pdMS_TO_TICKS(60));
}

static void map_point(int rx, int ry, int *x, int *y)
{
#if TOUCH_SWAP_XY
    const int t = rx;
    rx = ry;
    ry = t;
    const int xmax = s_ymax, ymax = s_xmax;
#else
    const int xmax = s_xmax, ymax = s_ymax;
#endif
    int sx = xmax > 0 ? rx * PDA_EPD_W / xmax : rx;
    int sy = ymax > 0 ? ry * PDA_EPD_H / ymax : ry;
#if TOUCH_FLIP_X
    sx = PDA_EPD_W - 1 - sx;
#endif
#if TOUCH_FLIP_Y
    sy = PDA_EPD_H - 1 - sy;
#endif
    sx = sx < 0 ? 0 : sx >= PDA_EPD_W ? PDA_EPD_W - 1 : sx;
    sy = sy < 0 ? 0 : sy >= PDA_EPD_H ? PDA_EPD_H - 1 : sy;
    /* panel (landscape) -> the portrait screen the UI draws */
    pda_epd_touch_to_screen(sx, sy, x, y);
}

static void touch_task(void *arg)
{
    (void)arg;
    gesture_state_t g;
    gesture_reset(&g);
    bool touching = false;
    int x = 0, y = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        if (s_asleep) {
            continue;
        }
        uint8_t st = 0;
        if (rd(REG_STATUS, &st, 1) != ESP_OK) {
            s_errors++;
            continue;
        }
        s_reads++;
        if (st & 0x80) { /* buffer ready: a new sample */
            const int n = st & 0x0f;
            touching = n > 0;
            if (touching) {
                uint8_t p[8];
                if (rd(REG_POINT1, p, sizeof(p)) == ESP_OK) {
                    const int rx = p[1] | (p[2] << 8), ry = p[3] | (p[4] << 8);
                    map_point(rx, ry, &x, &y);
                    if (esp_timer_get_time() / 1000 < s_trace_until) {
                        printf("touch raw %d,%d -> %d,%d (%d points)\n", rx, ry, x, y, n);
                    }
                }
            }
            wr8(REG_STATUS, 0);
        }
        /* Without a new sample the finger state stays as last reported. */
        const gesture_t ge = gesture_feed(&g, touching, x, y, esp_timer_get_time() / 1000);
        if (ge.kind != GESTURE_NONE) {
            s_gestures++;
            ESP_LOGI(TAG, "%s at %d,%d", gesture_name(ge.kind), ge.x, ge.y);
            if (s_cb) {
                s_cb(&ge, s_ctx);
            }
        }
    }
}

esp_err_t touch_init(i2c_master_bus_handle_t bus, touch_cb_t cb, void *ctx)
{
    s_cb = cb;
    s_ctx = ctx;
    hw_reset();
    uint8_t addr = T5_ADDR_TOUCH;
    if (i2c_master_probe(bus, addr, 50) != ESP_OK) {
        addr = T5_ADDR_TOUCH_ALT;
        if (i2c_master_probe(bus, addr, 50) != ESP_OK) {
            ESP_LOGE(TAG, "GT911 not found at 0x5D or 0x14");
            return ESP_ERR_NOT_FOUND;
        }
    }
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &s_dev);
    if (err != ESP_OK) {
        return err;
    }
    uint8_t id[4] = {0};
    err = rd(REG_PRODUCT_ID, id, 4);
    if (err != ESP_OK) {
        return err;
    }
    memcpy(s_id, id, 4);
    s_id[4] = 0;
    uint8_t res[4];
    if (rd(REG_CONFIG_RES, res, 4) == ESP_OK) {
        const int xm = res[0] | (res[1] << 8), ym = res[2] | (res[3] << 8);
        if (xm > 0 && ym > 0) {
            s_xmax = xm;
            s_ymax = ym;
        }
    }
    wr8(REG_STATUS, 0);
    ESP_LOGI(TAG, "GT%s at 0x%02x, resolution %dx%d", s_id, addr, s_xmax, s_ymax);
    if (xTaskCreate(touch_task, "touch", TASK_STACK, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void touch_sleep(void)
{
    s_asleep = true;
    if (s_dev) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS * 2)); /* let a poll in progress finish */
        wr8(0x8040, 0x05); /* command register: screen off (sleep) */
    }
}

void touch_trace(int ms)
{
    s_trace_until = esp_timer_get_time() / 1000 + ms;
}

void touch_print(void)
{
    printf("touch: GT%s, panel %dx%d, %lu polls, %lu errors, %lu gestures\n", s_id, s_xmax,
           s_ymax, (unsigned long)s_reads, (unsigned long)s_errors, (unsigned long)s_gestures);
}
