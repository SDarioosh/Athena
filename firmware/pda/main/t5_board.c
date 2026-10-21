#include "t5_board.h"

#include <inttypes.h>
#include <stdio.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/rtc_io.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_system.h"
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
/* Front light PWM. The XTAL clock keeps the frequency fixed while DFS changes the APB
 * clock. 1 kHz, 10 bits; the LED driver's EN pin takes it directly. */
#define BL_TIMER LEDC_TIMER_0
#define BL_CHANNEL LEDC_CHANNEL_0
#define BL_HZ 1000
#define BL_BITS LEDC_TIMER_10_BIT
#define BL_MAX ((1 << 10) - 1)

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
static bool s_bl_ready;
static bool s_bl_held; /* timer wake: the pin is still held low from the sleep */

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

/* Front light off until the app sets its level (the LED driver costs tens of mA). */
static void backlight_init(void)
{
    const ledc_timer_config_t bt = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = BL_BITS,
        .timer_num = BL_TIMER,
        .freq_hz = BL_HZ,
        .clk_cfg = LEDC_USE_XTAL_CLK,
    };
    const ledc_channel_config_t bc = {
        .gpio_num = T5_BACKLIGHT,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = BL_CHANNEL,
        .timer_sel = BL_TIMER,
        .duty = 0,
    };
    if (ledc_timer_config(&bt) == ESP_OK && ledc_channel_config(&bc) == ESP_OK) {
        s_bl_ready = true;
    } else {
        ESP_LOGW(TAG, "front light PWM unavailable: kept off");
        gpio_config_t bl = {.pin_bit_mask = 1ULL << T5_BACKLIGHT, .mode = GPIO_MODE_OUTPUT};
        gpio_config(&bl);
        gpio_set_level(T5_BACKLIGHT, 0);
    }
}

/* t5_board_restart_awake() leaves this for the next boot (survives a software reset). */
#define RESTART_AWAKE_MAGIC 0x57414b45u /* "WAKE" */
static RTC_NOINIT_ATTR uint32_t s_restart_awake;
static t5_wake_t s_wake;
static bool s_wake_known;

t5_wake_t t5_board_wake_reason(void)
{
    if (!s_wake_known) {
        s_wake_known = true;
        const esp_sleep_wakeup_cause_t c = esp_sleep_get_wakeup_cause();
        if (c == ESP_SLEEP_WAKEUP_TIMER) {
            s_wake = T5_WAKE_TIMER;
        } else if (c == ESP_SLEEP_WAKEUP_EXT0) {
            s_wake = T5_WAKE_BUTTON;
        } else if (esp_reset_reason() == ESP_RST_SW && s_restart_awake == RESTART_AWAKE_MAGIC) {
            s_wake = T5_WAKE_BUTTON;
        } else {
            s_wake = T5_WAKE_RESET;
        }
        s_restart_awake = 0;
    }
    return s_wake;
}

esp_err_t t5_board_init(void)
{
    const bool timer_wake = t5_board_wake_reason() == T5_WAKE_TIMER;
    /* After a wake from deep sleep the BOOT pad is still an RTC pin: back to the digital
     * GPIO matrix (EXT0 is set up again before the next sleep). */
    rtc_gpio_deinit(T5_BOOT_BTN);
    gpio_config_t boot = {
        .pin_bit_mask = 1ULL << T5_BOOT_BTN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&boot);

    if (timer_wake) {
        /* A sync wake: the backlight pin stays held low as t5_board_deep_sleep() left it
         * (releasing it would let EN float, and the LEDs flash on). No PWM. */
        s_bl_held = true;
    } else {
        gpio_hold_dis(T5_BACKLIGHT);
        gpio_deep_sleep_hold_dis();
        backlight_init();
    }

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
    uint8_t in[2] = {0}, cfg1 = 0;
    reg_read(s_pca, PCA_IN0, in, 2);
    reg_read(s_pca, PCA_CFG1, &cfg1, 1);
    ESP_LOGI(TAG, "PCA9535 in0 0x%02x in1 0x%02x cfg1 0x%02x; BOOT %s, side %s", in[0], in[1], cfg1,
             t5_button_is_down(T5_BUTTON_BOOT) ? "down" : "up",
             t5_button_is_down(T5_BUTTON_SIDE) ? "down" : "up");
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

/* ---- button inputs ---- */

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

/* ---- front light ---- */

void t5_backlight_set(int percent)
{
    if (!s_bl_ready) {
        return;
    }
    percent = percent < 0 ? 0 : percent > 100 ? 100 : percent;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, BL_CHANNEL, (uint32_t)(BL_MAX * percent / 100));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, BL_CHANNEL);
}

/* ---- deep sleep ---- */

static void wait_boot_released(int max_ms)
{
    for (int ms = 0; gpio_get_level(T5_BOOT_BTN) == 0 && (max_ms < 0 || ms < max_ms);
         ms += BUTTON_POLL_MS) {
        vTaskDelay(pdMS_TO_TICKS(BUTTON_POLL_MS));
    }
    vTaskDelay(pdMS_TO_TICKS(50)); /* debounce the release */
}

void t5_board_deep_sleep(uint64_t timer_us)
{
    /* Light off, and the pin held low through sleep: a floating EN would light the LEDs.
     * In a timer wake it is still held from the last sleep; holding it again is a no-op. */
    if (!s_bl_held) {
        if (s_bl_ready) {
            ledc_stop(LEDC_LOW_SPEED_MODE, BL_CHANNEL, 0);
        }
        const gpio_config_t bl = {.pin_bit_mask = 1ULL << T5_BACKLIGHT, .mode = GPIO_MODE_OUTPUT};
        gpio_set_level(T5_BACKLIGHT, 0);
        gpio_config(&bl); /* back from the LEDC signal to a plain GPIO */
        gpio_set_level(T5_BACKLIGHT, 0);
    }
    gpio_hold_en(T5_BACKLIGHT);
    gpio_deep_sleep_hold_en();

    /* The hold that sent us here: sleeping with BOOT down would wake at once. */
    wait_boot_released(-1);

    rtc_gpio_pullup_en(T5_BOOT_BTN);
    rtc_gpio_pulldown_dis(T5_BOOT_BTN);
    esp_sleep_enable_ext0_wakeup(T5_BOOT_BTN, 0);
    if (timer_us > 0) {
        esp_sleep_enable_timer_wakeup(timer_us);
        ESP_LOGI(TAG, "deep sleep: press BOOT to wake; sync wake in %" PRIu64 " s",
                 timer_us / 1000000);
    } else {
        ESP_LOGI(TAG, "deep sleep: press BOOT to wake");
    }
    esp_deep_sleep_start();
}

bool t5_board_woke_from_sleep(void)
{
    return t5_board_wake_reason() != T5_WAKE_RESET;
}

void t5_board_side_button_input(void)
{
    uint8_t cfg = 0;
    if (reg_read(s_pca, PCA_CFG1, &cfg, 1) == ESP_OK) {
        reg_write8(s_pca, PCA_CFG1, (uint8_t)(cfg | T5_PCA_P1_BUTTON));
    }
}

void t5_board_restart_awake(void)
{
    /* BOOT is a strapping pin: don't reset while it's down. */
    wait_boot_released(5000);
    s_restart_awake = RESTART_AWAKE_MAGIC;
    ESP_LOGI(TAG, "BOOT pressed during a sync wake: restarting into the UI");
    esp_restart();
}

/* ---- buttons ---- */

static void button_task(void *arg)
{
    (void)arg;
    struct {
        bool down, long_sent;
        int64_t since;
    } st[2] = {0};
    /* A button already down when the task starts (BOOT still held from the press that woke
     * the T5) reports nothing: neither a long press nor, on release, a short one. */
    for (int b = 0; b < 2; b++) {
        st[b].down = t5_button_is_down((t5_button_t)b);
        st[b].long_sent = st[b].down;
    }
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
