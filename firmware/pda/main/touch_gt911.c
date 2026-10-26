#include "touch_gt911.h"

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "pda_epd.h"
#include "t5_pins.h"
#include "touch_idle.h"

static const char *TAG = "touch";

#define POLL_MS 25 /* while a finger is down */
#define TASK_STACK 3072
#define REG_PRODUCT_ID 0x8140
#define REG_STATUS 0x814E
#define REG_POINT1 0x814F
#define REG_CONFIG_RES 0x8048 /* x max u16 LE, y max u16 LE */
#define REG_CONFIG_TRIG 0x804D /* module switch 1: bits 1:0 = INT trigger mode */

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
static volatile bool s_asleep, s_parked;
static SemaphoreHandle_t s_int_sem; /* given by the INT interrupt (and by touch_sleep) */
static bool s_int_ready;            /* the INT interrupt is installed */
static int s_int_level = 0;         /* INT level meaning "data ready" */
static uint8_t s_trig = 0xff;       /* 0x804D as read at init */
static touch_idle_t s_idle;
static volatile uint32_t s_irqs;
static uint32_t s_idle_polls, s_unarmed;

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

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* ---- INT line ---- */

static void int_isr(void *arg)
{
    (void)arg;
    gpio_intr_disable(T5_TOUCH_INT); /* level interrupt: once, until the task re-arms it */
    s_irqs++;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_int_sem, &woken);
    if (woken) {
        portYIELD_FROM_ISR();
    }
}

/* Enable the INT interrupt and the light-sleep wake on it, if the line is idle. A line
 * already at its active level (level mode before the buffer is cleared, or the polarity read
 * wrong) would fire at once and keep waking the chip: then the task polls instead. */
static bool int_arm(void)
{
    if (!s_int_ready || gpio_get_level(T5_TOUCH_INT) == s_int_level) {
        return false;
    }
    xSemaphoreTake(s_int_sem, 0); /* a give left from the last touch */
    gpio_wakeup_enable(T5_TOUCH_INT, s_int_level ? GPIO_INTR_HIGH_LEVEL : GPIO_INTR_LOW_LEVEL);
    gpio_intr_enable(T5_TOUCH_INT);
    return true;
}

static void int_disarm(void)
{
    if (s_int_ready) {
        gpio_intr_disable(T5_TOUCH_INT);
        gpio_wakeup_disable(T5_TOUCH_INT);
    }
}

/* No finger down: block until INT, or until the safety-net poll is due. Returns whether INT
 * woke us. */
static bool wait_for_touch(void)
{
    static int unarmed_in_a_row;
    const bool armed = int_arm();
    const int wait = touch_idle_wait_ms(&s_idle, armed);
    bool by_int = false;
    if (armed) {
        by_int = xSemaphoreTake(s_int_sem, pdMS_TO_TICKS(wait)) == pdTRUE;
        int_disarm();
        if (!by_int) {
            s_idle_polls++;
        }
        unarmed_in_a_row = 0;
    } else {
        /* touch_sleep() gives the semaphore to end this wait early */
        xSemaphoreTake(s_int_sem, pdMS_TO_TICKS(wait));
        s_unarmed++;
        if (s_int_ready && ++unarmed_in_a_row == 1000 / TOUCH_UNARMED_POLL_MS) {
            ESP_LOGW(TAG, "INT stays at its active level (%s): polling every %d ms",
                     touch_trig_name(s_trig), TOUCH_UNARMED_POLL_MS);
        }
    }
    return by_int;
}

/* One touch, from its first sample until the finger is up and the gesture is settled:
 * polled every POLL_MS (long-press timing and swipes need the samples). */
static void track_touch(gesture_state_t *g, bool by_int)
{
    bool touching = false, first = true;
    int x = 0, y = 0;
    int64_t last_sample = now_ms();
    for (;;) {
        uint8_t st = 0;
        if (rd(REG_STATUS, &st, 1) != ESP_OK) {
            s_errors++;
        } else {
            s_reads++;
            if (st & 0x80) { /* buffer ready: a new sample */
                const int n = st & 0x0f;
                uint8_t p[8] = {0};
                if (n > 0 && rd(REG_POINT1, p, sizeof(p)) != ESP_OK) {
                    /* no position: drop the sample, as for a failed status read; on a touch's
                     * first sample x,y are still 0,0, and the next good one made a swipe */
                    s_errors++;
                } else {
                    touching = n > 0;
                    last_sample = now_ms();
                    if (touching) {
                        const int rx = p[1] | (p[2] << 8), ry = p[3] | (p[4] << 8);
                        map_point(rx, ry, &x, &y);
                        if (now_ms() < s_trace_until) {
                            printf("touch raw %d,%d -> %d,%d (%d points)\n", rx, ry, x, y, n);
                        }
                    }
                }
                wr8(REG_STATUS, 0);
            }
        }
        if (first) {
            first = false;
            if (!touching) {
                return; /* an INT or a poll with no finger: back to waiting */
            }
            const bool trusted = touch_idle_trusted(&s_idle);
            touch_idle_note(&s_idle, by_int);
            if (!by_int && s_int_ready && s_idle.by_poll == 1) {
                ESP_LOGW(TAG, "a poll found a touch INT didn't report: safety-net poll every %d ms",
                         TOUCH_IDLE_POLL_MS);
            } else if (!trusted && touch_idle_trusted(&s_idle)) {
                ESP_LOGI(TAG, "INT works: safety-net poll every %d ms", TOUCH_IDLE_TRUSTED_MS);
            }
        }
        /* Without a new sample the finger state stays as last reported, except that a lost
         * release frame mustn't keep us polling forever. */
        if (touching && now_ms() - last_sample > TOUCH_STALE_MS) {
            touching = false;
        }
        const gesture_t ge = gesture_feed(g, touching, x, y, now_ms());
        if (ge.kind != GESTURE_NONE) {
            s_gestures++;
            ESP_LOGI(TAG, "%s at %d,%d", gesture_name(ge.kind), ge.x, ge.y);
            if (s_cb) {
                s_cb(&ge, s_ctx);
            }
        }
        if (!touching && !g->down) {
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        if (s_asleep) {
            return;
        }
    }
}

static void touch_task(void *arg)
{
    (void)arg;
    gesture_state_t g;
    gesture_reset(&g);
    for (;;) {
        if (s_asleep) {
            int_disarm();
            s_parked = true;
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        const bool by_int = wait_for_touch();
        if (!s_asleep) {
            track_touch(&g, by_int);
        }
    }
}

/* INT as a level interrupt at the level 0x804D names (GPIO wake-up from light sleep only
 * knows levels; the edge modes still hold the line a while, or a poll catches it). */
static void int_init(void)
{
    s_int_level = touch_trig_active_level(s_trig);
    if (gpio_isr_handler_add(T5_TOUCH_INT, int_isr, NULL) != ESP_OK) {
        ESP_LOGW(TAG, "no INT interrupt (GPIO ISR service missing?): polling every %d ms",
                 TOUCH_UNARMED_POLL_MS);
        return;
    }
    gpio_intr_disable(T5_TOUCH_INT);
    gpio_set_intr_type(T5_TOUCH_INT, s_int_level ? GPIO_INTR_HIGH_LEVEL : GPIO_INTR_LOW_LEVEL);
    esp_sleep_enable_gpio_wakeup();
    s_int_ready = true;
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
    if (rd(REG_CONFIG_TRIG, &s_trig, 1) != ESP_OK) {
        s_trig = 0x01; /* unreadable: assume the GT911's usual falling edge */
    }
    wr8(REG_STATUS, 0);
    ESP_LOGI(TAG, "GT%s at 0x%02x, resolution %dx%d, INT %s (0x804D = 0x%02x)", s_id, addr,
             s_xmax, s_ymax, touch_trig_name(s_trig), s_trig);
    s_int_sem = xSemaphoreCreateBinary();
    if (!s_int_sem) {
        return ESP_ERR_NO_MEM;
    }
    int_init();
    if (xTaskCreate(touch_task, "touch", TASK_STACK, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void touch_sleep(void)
{
    s_asleep = true;
    if (s_dev) {
        if (s_int_sem) { /* NULL when touch_init() failed after adding the device */
            xSemaphoreGive(s_int_sem); /* end an idle wait now */
        }
        for (int i = 0; i < 20 && !s_parked; i++) {
            vTaskDelay(pdMS_TO_TICKS(10)); /* let a poll in progress finish */
        }
        int_disarm(); /* INT mustn't wake the T5 from deep sleep */
        wr8(0x8040, 0x05); /* command register: screen off (sleep) */
    }
}

void touch_trace(int ms)
{
    s_trace_until = esp_timer_get_time() / 1000 + ms;
}

void touch_print(void)
{
    printf("touch: GT%s, panel %dx%d, %lu reads, %lu errors, %lu gestures\n", s_id, s_xmax,
           s_ymax, (unsigned long)s_reads, (unsigned long)s_errors, (unsigned long)s_gestures);
    printf("touch INT: %s (0x804D = 0x%02x), line %s now; %lu interrupts; touches started by "
           "INT %lu, by poll %lu; %lu idle polls, %lu waits unarmed; safety net %d ms\n",
           s_int_ready ? touch_trig_name(s_trig) : "not installed", s_trig,
           gpio_get_level(T5_TOUCH_INT) ? "high" : "low", (unsigned long)s_irqs,
           (unsigned long)s_idle.by_int, (unsigned long)s_idle.by_poll,
           (unsigned long)s_idle_polls, (unsigned long)s_unarmed,
           touch_idle_wait_ms(&s_idle, true));
}
