#include "t5_board.h"

#include <inttypes.h>
#include <stdio.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "isotime.h"
#include "t5_pins.h"

static const char *TAG = "board";

#define I2C_HZ 400000
#define I2C_TIMEOUT_MS 50
#define BUTTON_POLL_MS 20
#define BUTTON_LONG_MS 1000
#define BUTTON_TASK_STACK 3072

/* PCA9535 registers */
#define PCA_IN0 0x00
#define PCA_IN1 0x01
#define PCA_OUT0 0x02
#define PCA_CFG0 0x06
#define PCA_CFG1 0x07

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_pca, s_rtc, s_gauge, s_charger;
static t5_button_cb_t s_cb;
static void *s_cb_ctx;

static esp_err_t add_dev(uint8_t addr, i2c_master_dev_handle_t *out)
{
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = I2C_HZ,
    };
    return i2c_master_bus_add_device(s_bus, &cfg, out);
}

static esp_err_t reg_read(i2c_master_dev_handle_t d, uint8_t reg, uint8_t *buf, size_t len)
{
    return i2c_master_transmit_receive(d, &reg, 1, buf, len, I2C_TIMEOUT_MS);
}

static esp_err_t reg_write8(i2c_master_dev_handle_t d, uint8_t reg, uint8_t val)
{
    const uint8_t b[2] = {reg, val};
    return i2c_master_transmit(d, b, sizeof(b), I2C_TIMEOUT_MS);
}

esp_err_t t5_board_init(void)
{
    /* Front light off: the PDA reads in daylight; the LED driver would cost ~tens of mA. */
    gpio_config_t bl = {.pin_bit_mask = 1ULL << T5_BACKLIGHT, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&bl);
    gpio_set_level(T5_BACKLIGHT, 0);

    gpio_config_t boot = {
        .pin_bit_mask = 1ULL << T5_BOOT_BTN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&boot);

    const i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = T5_I2C_SDA,
        .scl_io_num = T5_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus: %s", esp_err_to_name(err));
        return err;
    }
    const struct {
        uint8_t addr;
        i2c_master_dev_handle_t *h;
    } devs[] = {
        {T5_ADDR_PCA9535, &s_pca},
        {T5_ADDR_RTC, &s_rtc},
        {T5_ADDR_GAUGE, &s_gauge},
        {T5_ADDR_CHARGER, &s_charger},
    };
    for (size_t i = 0; i < sizeof(devs) / sizeof(devs[0]); i++) {
        err = add_dev(devs[i].addr, devs[i].h);
        if (err != ESP_OK) {
            return err;
        }
    }

    /* LoRa/GPS rail off: output register first, then make the pin an output. */
    uint8_t v = 0;
    if (reg_read(s_pca, PCA_OUT0, &v, 1) == ESP_OK) {
        reg_write8(s_pca, PCA_OUT0, (uint8_t)(v & ~T5_PCA_P0_LORA_EN));
    }
    if (reg_read(s_pca, PCA_CFG0, &v, 1) == ESP_OK) {
        err = reg_write8(s_pca, PCA_CFG0, (uint8_t)(v & ~T5_PCA_P0_LORA_EN));
    } else {
        err = ESP_ERR_NOT_FOUND;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "PCA9535 not answering: %s", esp_err_to_name(err));
        return err;
    }
    t5_board_log_i2c();
    return ESP_OK;
}

i2c_master_bus_handle_t t5_board_i2c(void)
{
    return s_bus;
}

void t5_board_log_i2c(void)
{
    static const struct {
        uint8_t addr;
        const char *name;
    } chips[] = {
        {T5_ADDR_PCA9535, "PCA9535"}, {T5_ADDR_RTC, "PCF8563"},   {T5_ADDR_GAUGE, "BQ27220"},
        {T5_ADDR_TOUCH, "GT911"},     {T5_ADDR_TOUCH_ALT, "GT911 (alt)"},
        {T5_ADDR_TPS, "TPS65185"},    {T5_ADDR_CHARGER, "BQ25896"},
    };
    for (size_t i = 0; i < sizeof(chips) / sizeof(chips[0]); i++) {
        const esp_err_t err = i2c_master_probe(s_bus, chips[i].addr, I2C_TIMEOUT_MS);
        ESP_LOGI(TAG, "i2c 0x%02x %-11s %s", chips[i].addr, chips[i].name,
                 err == ESP_OK ? "ok" : "absent");
    }
}

/* ---- battery ---- */

static esp_err_t gauge_read16(uint8_t cmd, uint16_t *out)
{
    uint8_t b[2];
    const esp_err_t err = reg_read(s_gauge, cmd, b, 2);
    if (err == ESP_OK) {
        *out = (uint16_t)(b[0] | (b[1] << 8));
    }
    return err;
}

esp_err_t t5_battery_read(t5_battery_t *out)
{
    *out = (t5_battery_t){.percent = -1};
    uint16_t mv = 0, ma = 0, soc = 0;
    esp_err_t err = gauge_read16(0x08, &mv); /* Voltage */
    if (err == ESP_OK) {
        gauge_read16(0x0C, &ma);  /* Current, signed */
        gauge_read16(0x2C, &soc); /* StateOfCharge */
        out->millivolts = mv;
        out->milliamps = (int16_t)ma;
        out->present = mv > 2500 && mv < 4600;
        out->percent = out->present && soc <= 100 ? soc : -1;
    }
    uint8_t st = 0; /* BQ25896 REG0B: VBUS_STAT[7:5], CHRG_STAT[4:3], PG_STAT[2] */
    const esp_err_t cerr = reg_read(s_charger, 0x0B, &st, 1);
    if (cerr == ESP_OK) {
        const int chrg = (st >> 3) & 3;
        out->charging = chrg == 1 || chrg == 2;
        out->vbus = (st & 0x04) != 0;
    }
    return err == ESP_OK ? cerr : err;
}

/* ---- RTC (PCF8563) ---- */

static int bcd(uint8_t v)
{
    return (v >> 4) * 10 + (v & 0x0f);
}

static uint8_t to_bcd(int v)
{
    return (uint8_t)(((v / 10) << 4) | (v % 10));
}

esp_err_t t5_rtc_get(int64_t *epoch, bool *valid)
{
    uint8_t r[7]; /* 0x02 seconds .. 0x08 years */
    const esp_err_t err = reg_read(s_rtc, 0x02, r, sizeof(r));
    if (err != ESP_OK) {
        return err;
    }
    const isotime_fields_t f = {
        .second = bcd(r[0] & 0x7f),
        .minute = bcd(r[1] & 0x7f),
        .hour = bcd(r[2] & 0x3f),
        .day = bcd(r[3] & 0x3f),
        .month = bcd(r[5] & 0x1f),
        /* The century bit flips when years roll 99 -> 00; we write it as 0 for 20xx. */
        .year = 2000 + bcd(r[6]) + ((r[5] & 0x80) ? 100 : 0),
    };
    const bool ok = isotime_from_fields(&f, epoch);
    *valid = ok && !(r[0] & 0x80) && isotime_is_plausible(*epoch);
    if (!ok) {
        *epoch = 0;
    }
    return ESP_OK;
}

esp_err_t t5_rtc_set(int64_t epoch)
{
    isotime_fields_t f;
    isotime_to_fields(epoch, &f);
    if (f.year < 2000 || f.year > 2099) {
        return ESP_ERR_INVALID_ARG;
    }
    /* Weekday: 1970-01-01 was a Thursday (4). Not used by us, kept correct anyway. */
    const int wday = (int)(((epoch / 86400) % 7 + 4) % 7);
    const uint8_t b[8] = {
        0x02, to_bcd(f.second), to_bcd(f.minute), to_bcd(f.hour), to_bcd(f.day),
        (uint8_t)wday, to_bcd(f.month), to_bcd(f.year - 2000), /* writing seconds clears VL */
    };
    return i2c_master_transmit(s_rtc, b, sizeof(b), I2C_TIMEOUT_MS);
}

/* ---- buttons ---- */

static bool side_down(void)
{
    uint8_t v = 0xff;
    if (reg_read(s_pca, PCA_IN1, &v, 1) != ESP_OK) {
        return false;
    }
    return (v & T5_PCA_P1_BUTTON) == 0;
}

bool t5_button_is_down(t5_button_t button)
{
    return button == T5_BUTTON_BOOT ? gpio_get_level(T5_BOOT_BTN) == 0 : side_down();
}

static void button_task(void *arg)
{
    (void)arg;
    struct {
        bool down, long_sent;
        int64_t since;
    } st[2] = {0};
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(BUTTON_POLL_MS));
        const int64_t now = esp_timer_get_time() / 1000;
        for (int b = 0; b < 2; b++) {
            const bool down = t5_button_is_down((t5_button_t)b);
            if (down && !st[b].down) {
                st[b].since = now;
                st[b].long_sent = false;
            } else if (down && !st[b].long_sent && now - st[b].since >= BUTTON_LONG_MS) {
                st[b].long_sent = true;
                if (s_cb) {
                    s_cb((t5_button_t)b, T5_PRESS_LONG, s_cb_ctx);
                }
            } else if (!down && st[b].down && !st[b].long_sent && now - st[b].since >= 30) {
                if (s_cb) {
                    s_cb((t5_button_t)b, T5_PRESS_SHORT, s_cb_ctx);
                }
            }
            st[b].down = down;
        }
    }
}

esp_err_t t5_board_start_buttons(t5_button_cb_t cb, void *ctx)
{
    s_cb = cb;
    s_cb_ctx = ctx;
    /* epdiy made every port-1 pin but PWRGOOD and INT an output; IO1_2 is the button. */
    uint8_t cfg = 0;
    esp_err_t err = reg_read(s_pca, PCA_CFG1, &cfg, 1);
    if (err == ESP_OK) {
        err = reg_write8(s_pca, PCA_CFG1, (uint8_t)(cfg | T5_PCA_P1_BUTTON));
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "side button config: %s", esp_err_to_name(err));
    }
    if (xTaskCreate(button_task, "buttons", BUTTON_TASK_STACK, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
