#include "t5_board.h"

#include <inttypes.h>
#include <stdio.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/rtc_io.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "isotime.h"
#include "t5_pins.h"

static const char *TAG = "board";

#define I2C_HZ 400000
#define I2C_TIMEOUT_MS 50
/* Both buttons are polled: the side button sits on the PCA9535, whose INT line epdiy owns
 * (GPIO38), and BOOT costs nothing extra in the same loop (no I2C). Every poll is a wake from
 * light sleep and, for the side button, an I2C read, so this is the T5's main idle cost:
 * 50 ms is 20 wakes a second instead of 50. A quick human press lasts 80-100 ms or more, so it
 * still spans at least one poll; the 1 s long press is timed to within 50 ms. */
#define BUTTON_POLL_MS 50
/* USB power (BQ25896 power good) is checked this often; while it is there the T5 stays out
 * of light sleep so the USB Serial/JTAG console keeps working. */
#define USB_CHECK_MS 1000
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
/* ESP_PM_NO_LIGHT_SLEEP while the front light is lit (LEDC stops in light sleep) and while
 * USB power is in (the USB Serial/JTAG console dies in light sleep). */
static esp_pm_lock_handle_t s_light_lock, s_usb_lock;
static bool s_light_held, s_usb_held;
static portMUX_TYPE s_pm_mux = portMUX_INITIALIZER_UNLOCKED;

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

/* ---- power management ---- */

static void pm_hold(esp_pm_lock_handle_t lock, bool *held, bool want)
{
    if (!lock) {
        return;
    }
    taskENTER_CRITICAL(&s_pm_mux);
    if (*held != want) {
        *held = want;
        if (want) {
            esp_pm_lock_acquire(lock);
        } else {
            esp_pm_lock_release(lock);
        }
    }
    taskEXIT_CRITICAL(&s_pm_mux);
}

/* DFS 240/40 MHz as CONFIG_PM_DFS_INIT_AUTO set it, now with automatic light sleep. 40 MHz is
 * safe: the LEDC and I2C run off the XTAL, an e-paper refresh holds CPU_FREQ_MAX (pda_epd.c,
 * which also keeps light sleep out of the refresh), and the BLE controller holds its own APB
 * lock while it works. */
static esp_err_t light_sleep_config(bool on)
{
    const esp_pm_config_t pm = {
        .max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
        .min_freq_mhz = 40,
        .light_sleep_enable = on,
    };
    return esp_pm_configure(&pm);
}

static bool usb_power(bool *vbus)
{
    uint8_t st = 0; /* BQ25896 REG0B: PG_STAT is bit 2 */
    if (reg_read(s_charger, 0x0B, &st, 1) != ESP_OK) {
        return false;
    }
    *vbus = (st & 0x04) != 0;
    return true;
}

static void usb_power_update(void)
{
    bool vbus = false;
    if (usb_power(&vbus) && vbus != s_usb_held) {
        pm_hold(s_usb_lock, &s_usb_held, vbus);
        ESP_LOGI(TAG, "USB power %s: light sleep %s", vbus ? "in" : "out",
                 vbus ? "off (console stays up)" : "on");
    }
}

static void power_init(void)
{
    if (esp_pm_lock_create(ESP_PM_NO_LIGHT_SLEEP, 0, "light", &s_light_lock) != ESP_OK ||
        esp_pm_lock_create(ESP_PM_NO_LIGHT_SLEEP, 0, "usb", &s_usb_lock) != ESP_OK) {
        ESP_LOGW(TAG, "PM locks unavailable: no light sleep");
        return;
    }
    /* Without the power-good read the console's state is unknown: keep it up. */
    bool vbus = true;
    usb_power(&vbus);
    pm_hold(s_usb_lock, &s_usb_held, vbus);
    const esp_err_t err = light_sleep_config(true);
    ESP_LOGI(TAG, "automatic light sleep %s; USB power %s",
             err == ESP_OK ? "on" : esp_err_to_name(err),
             vbus ? "in (no light sleep while it is)" : "out");
}

/* Before deep sleep: automatic light sleep leaves its timer wake-up armed (with the last
 * light sleep's few ms) and the touch INT's GPIO wake-up on; either would end deep sleep at
 * once. A timed deep-sleep wake must be armed after this. */
static void light_sleep_off(void)
{
    light_sleep_config(false); /* also disables the timer wake-up source */
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
}

void t5_board_print_power(void)
{
    esp_pm_config_t pm = {0};
    esp_pm_get_configuration(&pm);
    printf("power: CPU %d-%d MHz, automatic light sleep %s; kept awake by front light %s, USB %s\n",
           pm.min_freq_mhz, pm.max_freq_mhz, pm.light_sleep_enable ? "on" : "off",
           s_light_held ? "yes" : "no", s_usb_held ? "yes" : "no");
    esp_pm_dump_locks(stdout);
}

esp_err_t t5_board_init(void)
{
    /* After a wake from deep sleep: the BOOT pad is still an RTC pin and the backlight pin
     * is held low (t5_board_deep_sleep). Give both back to the digital GPIO matrix. */
    rtc_gpio_deinit(T5_BOOT_BTN);
    gpio_hold_dis(T5_BACKLIGHT);
    gpio_deep_sleep_hold_dis();

    /* Front light off until the app sets its level (the LED driver costs tens of mA). */
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
    power_init();
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
    if (percent > 0) {
        pm_hold(s_light_lock, &s_light_held, true); /* the PWM stops in light sleep */
    }
    ledc_set_duty(LEDC_LOW_SPEED_MODE, BL_CHANNEL, (uint32_t)(BL_MAX * percent / 100));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, BL_CHANNEL);
    if (percent == 0) {
        pm_hold(s_light_lock, &s_light_held, false); /* duty 0: the pin stays low */
    }
}

/* ---- deep sleep ---- */

void t5_board_deep_sleep(void)
{
    light_sleep_off();
    /* Light off, and the pin held low through sleep: a floating EN would light the LEDs. */
    if (s_bl_ready) {
        ledc_stop(LEDC_LOW_SPEED_MODE, BL_CHANNEL, 0);
    }
    const gpio_config_t bl = {.pin_bit_mask = 1ULL << T5_BACKLIGHT, .mode = GPIO_MODE_OUTPUT};
    gpio_set_level(T5_BACKLIGHT, 0);
    gpio_config(&bl); /* back from the LEDC signal to a plain GPIO */
    gpio_set_level(T5_BACKLIGHT, 0);
    gpio_hold_en(T5_BACKLIGHT);
    gpio_deep_sleep_hold_en();

    /* The hold that sent us here: sleeping with BOOT down would wake at once. */
    while (gpio_get_level(T5_BOOT_BTN) == 0) {
        vTaskDelay(pdMS_TO_TICKS(BUTTON_POLL_MS));
    }
    vTaskDelay(pdMS_TO_TICKS(50)); /* debounce the release */

    rtc_gpio_pullup_en(T5_BOOT_BTN);
    rtc_gpio_pulldown_dis(T5_BOOT_BTN);
    esp_sleep_enable_ext0_wakeup(T5_BOOT_BTN, 0);
    ESP_LOGI(TAG, "deep sleep: press BOOT to wake");
    esp_deep_sleep_start();
}

bool t5_board_woke_from_sleep(void)
{
    return esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0;
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
    int64_t usb_checked = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(BUTTON_POLL_MS));
        const int64_t now = esp_timer_get_time() / 1000;
        if (now - usb_checked >= USB_CHECK_MS) {
            usb_checked = now;
            usb_power_update();
        }
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
