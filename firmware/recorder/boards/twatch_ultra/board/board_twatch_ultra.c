/* LilyGO T-Watch Ultra board layer: implements main/board.h.
 *
 * Bring-up order follows docs/t-watch-ultra-plan.md §2.1: I2C, AXP2101 (every unused
 * rail off), XL9555, PCF85063A, haptics, buttons. The display and the SD card are powered
 * only while in use. */
#include "board.h"

#include <inttypes.h>
#include <string.h>

#include "axp2101.h"
#include "button_gesture.h"
#include "display.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "drv2605.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "isotime.h"
#include "nvs.h"
#include "pcf85063a.h"
#include "pins.h"
#include "sdcard.h"
#include "xl9555.h"

static const char *TAG = "board";

static board_button_cb_t s_button_cb;
static void *s_button_ctx;
static TaskHandle_t s_button_task;
static bool s_have_pmu, s_have_expander, s_have_rtc, s_have_haptic;

#define BUTTON_POLL_MS 10 /* only while a gesture is pending or the PMU IRQ line is low */
#define PWR_LONG_PRESS_MS 1000 /* PKEY_LONG IRQ time (AXP2101 reg 0x27 bits[5:4]) */
/* A PWR short press within this time of the previous one is ignored. On the owner's watch a
 * press that started a session was followed 0.5-1 s later by one or two more short presses
 * with real key edges (the start buzz shaking the key under the finger); console starts never
 * did it. Every short press, ignored or not, restarts the window. */
#define PWR_SHORT_GUARD_MS 1000

/* The PMU interrupts the app handles while awake. */
#define PMU_IRQ_MASK (AXP_IRQ_PKEY_SHORT | AXP_IRQ_PKEY_LONG | AXP_IRQ_VBUS_INSERT | \
                      AXP_IRQ_VBUS_REMOVE | AXP_IRQ_CHG_START | AXP_IRQ_CHG_DONE)
/* The ones that end standby: a PWR press, or USB power plugged in. */
#define PMU_WAKE_MASK (AXP_IRQ_PKEY_SHORT | AXP_IRQ_PKEY_LONG | AXP_IRQ_VBUS_INSERT)

/* AXP2101 fuel-gauge battery model for the T-Watch Ultra's 1100 mAh cell.
 * Copied from LilyGoLib src/LilyGoWatchUltra.cpp (BATTER_PARAMS), MIT License,
 * Copyright (c) 2024 ShenZhen XinYuan Electronic Technology Co., Ltd.
 * Written once (flag in NVS), like LilyGoLib does; writing it resets the gauge. */
static const uint8_t s_gauge_params[128] = {
    0x01, 0xf5, 0x40, 0x00, 0x1b, 0x1e, 0x28, 0x0f, 0x0c, 0x1e, 0x32, 0x02, 0x14, 0x05, 0x0a, 0x04,
    0x74, 0xfc, 0xf4, 0x0d, 0x43, 0x10, 0x52, 0xfb, 0xa6, 0x01, 0xea, 0x04, 0x64, 0x06, 0x52, 0x06,
    0x18, 0x0a, 0xe7, 0x0f, 0x9f, 0x0f, 0x51, 0x09, 0xf7, 0x0e, 0x89, 0x0e, 0x71, 0x04, 0x58, 0x04,
    0x43, 0x09, 0x32, 0x0e, 0x1c, 0x0e, 0x14, 0x09, 0x04, 0x0d, 0xe9, 0x0d, 0xde, 0x03, 0xc8, 0x03,
    0xb3, 0x08, 0x9d, 0x0d, 0x79, 0x0d, 0x3a, 0x07, 0xf5, 0x9e, 0x56, 0x47, 0x36, 0x20, 0x24, 0x17,
    0xc5, 0x98, 0x7e, 0x66, 0x4e, 0x44, 0x38, 0x1a, 0x12, 0x0a, 0xf6, 0x00, 0x00, 0xf6, 0x00, 0xf6,
    0x00, 0xfb, 0x00, 0x00, 0xfb, 0x00, 0x00, 0xfb, 0x00, 0x00, 0xf6, 0x00, 0x00, 0xf6, 0x00, 0xf6,
    0x00, 0xfb, 0x00, 0x00, 0xfb, 0x00, 0x00, 0xfb, 0x00, 0x00, 0xf6, 0x00, 0x00, 0xf6, 0x00, 0xf6,
};

static const struct {
    axp_rail_t rail;
    const char *what;
    int mv; /* -1: leave as shipped */
} s_rails[BOARD_RAIL_COUNT] = {
    [BOARD_RAIL_SD] = {AXP_ALDO1, "SD card", 3300},
    [BOARD_RAIL_DISPLAY] = {AXP_ALDO2, "display", 3300},
    [BOARD_RAIL_GPS] = {AXP_BLDO1, "GPS", -1},
    [BOARD_RAIL_LORA] = {AXP_ALDO3, "LoRa", -1},
    [BOARD_RAIL_NFC] = {AXP_DLDO1, "NFC", -1},
    [BOARD_RAIL_SPEAKER] = {AXP_BLDO2, "speaker", -1},
    [BOARD_RAIL_SENSOR] = {AXP_ALDO4, "sensor", -1},
};

const char *board_name(void)
{
    return "twatch_ultra";
}

/* ---------------------------------------------------------------- power ---- */

static void hiz_pin(int gpio)
{
    gpio_reset_pin(gpio);
    gpio_set_direction(gpio, GPIO_MODE_INPUT);
    gpio_set_pull_mode(gpio, GPIO_FLOATING);
}

static void gauge_params_once(void)
{
    nvs_handle_t nvs;
    uint8_t done = 0;
    if (nvs_open("board", NVS_READWRITE, &nvs) != ESP_OK) {
        ESP_LOGW(TAG, "nvs unavailable; gauge params not written");
        return;
    }
    nvs_get_u8(nvs, "gauge_v1", &done);
    if (!done) {
        const esp_err_t err = axp2101_gauge_write_params(s_gauge_params, sizeof(s_gauge_params));
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "fuel-gauge battery model written and verified");
            nvs_set_u8(nvs, "gauge_v1", 1);
            nvs_commit(nvs);
        } else {
            ESP_LOGW(TAG, "fuel-gauge battery model not accepted: %s", esp_err_to_name(err));
        }
    }
    nvs_close(nvs);
}

static esp_err_t pmu_init(void)
{
    ESP_RETURN_ON_ERROR(axp2101_init(I2C_ADDR_AXP2101), TAG, "axp2101");
    s_have_pmu = true;

    /* DC1 feeds the ESP32-S3: never touch it except to confirm it is on. */
    if (!axp2101_rail_is_enabled(AXP_DC1)) {
        ESP_LOGE(TAG, "DC1 reads as off while we run on it; PMU read is suspect");
    }
    /* Voltages for the rails we use, before switching them (both stay off for now). */
    axp2101_rail_set_mv(AXP_ALDO1, 3300);
    axp2101_rail_set_mv(AXP_ALDO2, 3300);

    /* Everything off except DC1. LilyGoLib turns most of these on; we don't. */
    const axp_rail_t off[] = {AXP_DC2, AXP_DC3, AXP_DC4, AXP_DC5, AXP_CPUSLDO, AXP_ALDO1,
                              AXP_ALDO2, AXP_ALDO3, AXP_ALDO4, AXP_BLDO1, AXP_BLDO2, AXP_DLDO1,
                              AXP_DLDO2};
    for (unsigned i = 0; i < sizeof(off) / sizeof(off[0]); i++) {
        axp2101_rail_enable(off[i], false);
    }

    axp2101_set_charge_current_ma(BOARD_CHARGE_CURRENT_MA);
    axp2101_set_charge_voltage_4v2();
    axp2101_chgled_off();
    /* Keep the RTC's backup cell charged so time survives a hardware power-off (LilyGoLib
     * does the same, 3.3 V). */
    axp2101_backup_charge_enable(3300);
    /* VBAT and the battery thermistor (the charger uses TS; LilyGoLib enables it too). */
    axp2101_adc_enable(0x03);
    gauge_params_once();

    /* PWR held ~1 s = PKEY_LONG (starts a voice note). The hardware power-off hold time
     * (bits[3:2], >= 4 s) is left as it is. */
    esp_err_t kerr = axp2101_set_pkey_long_ms(PWR_LONG_PRESS_MS);
    int long_ms = -1, off_ms = -1, on_ms = -1;
    axp2101_get_pkey_times(&long_ms, &off_ms, &on_ms);
    ESP_LOGI(TAG, "PWR key: long press %d ms (%s), power-off hold %d ms, power-on %d ms", long_ms,
             esp_err_to_name(kerr), off_ms, on_ms);

    axp2101_irq_enable(PMU_IRQ_MASK);
    uint32_t pending;
    axp2101_irq_read_clear(&pending);
    return ESP_OK;
}

esp_err_t board_rail_set(board_rail_t rail, bool on)
{
    ESP_RETURN_ON_FALSE(rail < BOARD_RAIL_COUNT, ESP_ERR_INVALID_ARG, TAG, "rail");
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_INVALID_STATE, TAG, "no pmu");
    ESP_RETURN_ON_ERROR(axp2101_rail_enable(s_rails[rail].rail, on), TAG, "set");
    ESP_LOGI(TAG, "rail %s (%s) -> %s", axp2101_rail_name(s_rails[rail].rail), s_rails[rail].what,
             on ? "on" : "off");
    return ESP_OK;
}

bool board_rail_is_on(board_rail_t rail)
{
    return rail < BOARD_RAIL_COUNT && s_have_pmu && axp2101_rail_is_enabled(s_rails[rail].rail);
}

void board_log_power(void)
{
    if (!s_have_pmu) {
        ESP_LOGW(TAG, "no PMU");
        return;
    }
    ESP_LOGI(TAG, "power rails (read back from the AXP2101):");
    for (board_rail_t r = 0; r < BOARD_RAIL_COUNT; r++) {
        ESP_LOGI(TAG, "  %-8s %-7s %s", s_rails[r].what, axp2101_rail_name(s_rails[r].rail),
                 axp2101_rail_is_enabled(s_rails[r].rail) ? "ON" : "off");
    }
    ESP_LOGI(TAG, "  charge current %d mA (limit %d mA)", axp2101_get_charge_current_ma(),
             BOARD_CHARGE_CURRENT_MA);
    axp2101_log_registers();
}

esp_err_t board_standby_prepare(void)
{
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_NOT_SUPPORTED, TAG, "no pmu");
    /* Only the PMU's IRQ line wakes the chip: a PWR press or USB power. Not BOOT: GPIO0 is
     * a strapping pin, and held low through the wake reset it could start the ROM
     * downloader instead of the firmware. */
    ESP_RETURN_ON_ERROR(axp2101_irq_enable(PMU_WAKE_MASK), TAG, "irq mask");
    uint32_t pending = 0;
    axp2101_irq_read_clear(&pending);
    if (gpio_get_level(PIN_PMU_IRQ) == 0) {
        axp2101_irq_enable(PMU_IRQ_MASK);
        ESP_LOGW(TAG, "standby: PMU IRQ line still low");
        return ESP_ERR_INVALID_STATE;
    }
    /* Haptics, display and SD rails are already off; touch stays in reset (XL9555 keeps its
     * outputs, it is on DC1). Keep the IRQ line pulled up through deep sleep. */
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO); /* the light-sleep buttons */
    rtc_gpio_pullup_en(PIN_PMU_IRQ);
    rtc_gpio_pulldown_dis(PIN_PMU_IRQ);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    return esp_sleep_enable_ext1_wakeup_io(1ULL << PIN_PMU_IRQ, ESP_EXT1_WAKEUP_ANY_LOW);
}

esp_err_t board_battery_read(board_battery_t *out)
{
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_INVALID_STATE, TAG, "no pmu");
    axp_battery_t b;
    ESP_RETURN_ON_ERROR(axp2101_read_battery(&b), TAG, "read");
    out->present = b.battery_present;
    out->millivolts = b.millivolts;
    out->percent = b.percent;
    out->charging = b.charging;
    out->vbus = b.vbus_good;
    return ESP_OK;
}

void board_battery_limits(bg_limits_t *out)
{
    /* 1100 mAh LiPo with the AXP2101 fuel gauge. 5 % leaves ~1 h of recording as margin
     * for the card writes and the shutdown; 3.5 V under the recording load is near empty
     * whatever the gauge says. Recording is allowed again from 10 % or on USB. */
    *out = (bg_limits_t){.low_percent = 5, .low_mv = 3500, .resume_percent = 10};
}

/* ------------------------------------------------------------- expander ---- */

static esp_err_t expander_init(void)
{
    ESP_RETURN_ON_ERROR(xl9555_init(I2C_ADDR_XL9555), TAG, "xl9555");
    s_have_expander = true;
    /* Outputs low first, then direction, so nothing glitches high. */
    xl9555_write(EXP_PIN_HAPTIC_EN, false);
    xl9555_write(EXP_PIN_DISP_EN, false);
    xl9555_write(EXP_PIN_TOUCH_RST, false); /* touch held in reset: unused, saves its wake current */
    xl9555_set_direction(EXP_PIN_HAPTIC_EN, false);
    xl9555_set_direction(EXP_PIN_DISP_EN, false);
    xl9555_set_direction(EXP_PIN_TOUCH_RST, false);
    xl9555_set_direction(EXP_PIN_SD_DET, true);
    xl9555_set_direction(EXP_PIN_LORA_RF_SW, true); /* Hi-Z: the LoRa rail is off */
    return ESP_OK;
}

void board_log_expander(void)
{
    if (!s_have_expander) {
        ESP_LOGW(TAG, "no expander");
        return;
    }
    uint16_t in, out, dir;
    if (xl9555_read_all(&in, &out, &dir) != ESP_OK) {
        ESP_LOGW(TAG, "expander read failed");
        return;
    }
    ESP_LOGI(TAG, "XL9555 inputs=0x%04X outputs=0x%04X directions=0x%04X (1 = input)", in, out, dir);
    for (int pin = 0; pin < 16; pin++) {
        const char *name = "n.c.";
        switch (pin) {
        case EXP_PIN_HAPTIC_EN: name = "P06 M_EN (haptics enable)"; break;
        case EXP_PIN_DISP_EN: name = "P07 VCI_EN (display enable)"; break;
        case EXP_PIN_TOUCH_RST: name = "P10 TP_RST (touch reset; hw page calls it GPIO10)"; break;
        case 9: name = "P11 (not connected on the schematic)"; break;
        case EXP_PIN_SD_DET: name = "P12 SD_DET (low = card in; hw page calls it GPIO12)"; break;
        case EXP_PIN_LORA_RF_SW: name = "P13 LORA_SEL (RF switch)"; break;
        case EXP_PIN_SD_DET_ALT: name = "P14 (would be SD_DET if 'GPIO12' were an index)"; break;
        default: break;
        }
        ESP_LOGI(TAG, "  pin %2d: %s %s  %s", pin, (dir >> pin) & 1 ? "in " : "out",
                 (in >> pin) & 1 ? "HIGH" : "LOW ", name);
    }
    ESP_LOGI(TAG, "pin-map check: SD_DET = pin %d must read LOW with a card inserted and HIGH without",
             EXP_PIN_SD_DET);
}

/* ------------------------------------------------------------------ rtc ---- */

esp_err_t board_rtc_get(int64_t *epoch, bool *valid)
{
    ESP_RETURN_ON_FALSE(s_have_rtc, ESP_ERR_INVALID_STATE, TAG, "no rtc");
    pcf85063a_datetime_t dt;
    bool stopped = true;
    ESP_RETURN_ON_ERROR(pcf85063a_read(&dt, &stopped), TAG, "read");
    const isotime_fields_t f = {dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second};
    int64_t e = 0;
    const bool ok = isotime_from_fields(&f, &e);
    *epoch = ok ? e : 0;
    *valid = ok && !stopped && isotime_is_plausible(e);
    return ESP_OK;
}

esp_err_t board_rtc_set(int64_t epoch)
{
    ESP_RETURN_ON_FALSE(s_have_rtc, ESP_ERR_INVALID_STATE, TAG, "no rtc");
    isotime_fields_t f;
    isotime_to_fields(epoch, &f);
    const pcf85063a_datetime_t dt = {f.year, f.month, f.day, f.hour, f.minute, f.second};
    return pcf85063a_write(&dt);
}

/* -------------------------------------------------------- buttons/haptic ---- */

/* Both lines are level-low interrupts (and light-sleep wake sources). The ISR masks its
 * pin and wakes the task, which polls until the line is high again and unmasks it. */
static void IRAM_ATTR line_isr(void *arg)
{
    gpio_intr_disable((gpio_num_t)(intptr_t)arg);
    BaseType_t woken = pdFALSE;
    if (s_button_task) {
        vTaskNotifyGiveFromISR(s_button_task, &woken);
    }
    portYIELD_FROM_ISR(woken);
}

/* The PWR event being emitted (board_pwr_event_info()); written and read in the button task. */
static uint32_t s_pwr_irq;
static int32_t s_pwr_gap_ms;

void board_pwr_event_info(uint32_t *pmu_irq, int32_t *since_prev_ms)
{
    *pmu_irq = s_pwr_irq;
    *since_prev_ms = s_pwr_gap_ms;
}

static void emit(board_button_t b, board_press_t p)
{
    if (p != BOARD_PRESS_SHORT_IGNORED) { /* handle_pmu_irq() already said so */
        ESP_LOGI(TAG, "button %s %s", b == BOARD_BUTTON_PWR ? "PWR" : "BOOT",
                 p == BOARD_PRESS_SHORT ? "short" : p == BOARD_PRESS_LONG ? "long" : "double");
    }
    if (s_button_cb) {
        s_button_cb(b, p, s_button_ctx);
    }
}

static void handle_pmu_irq(void)
{
    uint32_t st = 0;
    if (axp2101_irq_read_clear(&st) != ESP_OK || st == 0) {
        return;
    }
    /* The raw bits go with every PWR event to the event log: PKEY_SHORT with no key edge
     * (PKEY_POSITIVE/NEGATIVE) would be a press the key never made. */
    s_pwr_irq = st;
    s_pwr_gap_ms = 0;
    if (st & AXP_IRQ_PKEY_SHORT) {
        static int64_t last_ms = INT64_MIN / 2;
        const int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - last_ms < PWR_SHORT_GUARD_MS) {
            s_pwr_gap_ms = (int32_t)(now_ms - last_ms);
            ESP_LOGI(TAG, "button PWR short ignored: %d ms after the previous one (PMU 0x%06" PRIx32 ")",
                     (int)s_pwr_gap_ms, st);
            emit(BOARD_BUTTON_PWR, BOARD_PRESS_SHORT_IGNORED);
        } else {
            ESP_LOGI(TAG, "PMU 0x%06" PRIx32 " with the PWR short press", st);
            emit(BOARD_BUTTON_PWR, BOARD_PRESS_SHORT);
        }
        last_ms = now_ms;
    }
    if (st & AXP_IRQ_PKEY_LONG) {
        ESP_LOGI(TAG, "PMU 0x%06" PRIx32 " with the PWR long press", st);
        emit(BOARD_BUTTON_PWR, BOARD_PRESS_LONG);
    }
    if (st & AXP_IRQ_VBUS_INSERT) {
        ESP_LOGI(TAG, "USB power connected");
    }
    if (st & AXP_IRQ_VBUS_REMOVE) {
        ESP_LOGI(TAG, "USB power removed");
    }
    if (st & AXP_IRQ_CHG_START) {
        ESP_LOGI(TAG, "charging started");
    }
    if (st & AXP_IRQ_CHG_DONE) {
        ESP_LOGI(TAG, "charging done");
    }
    const uint32_t other = st & ~(AXP_IRQ_PKEY_SHORT | AXP_IRQ_PKEY_LONG | AXP_IRQ_VBUS_INSERT |
                                  AXP_IRQ_VBUS_REMOVE | AXP_IRQ_CHG_START | AXP_IRQ_CHG_DONE);
    if (other) {
        ESP_LOGI(TAG, "PMU irq status 0x%06" PRIx32, other);
    }
}

static void button_task(void *arg)
{
    (void)arg;
    button_gesture_t boot;
    button_gesture_init(&boot);
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        /* Idle (no BOOT gesture pending, IRQ line high): unmask both lines and block, so the
         * chip can light sleep until one goes low. Otherwise poll. */
        const bool busy = !button_gesture_idle(&boot) || gpio_get_level(PIN_BOOT_BUTTON) == 0 ||
                          gpio_get_level(PIN_PMU_IRQ) == 0;
        if (!busy) {
            gpio_intr_enable(PIN_BOOT_BUTTON);
            gpio_intr_enable(PIN_PMU_IRQ);
        }
        ulTaskNotifyTake(pdTRUE, busy ? pdMS_TO_TICKS(BUTTON_POLL_MS) : portMAX_DELAY);
        if (gpio_get_level(PIN_PMU_IRQ) == 0) {
            handle_pmu_irq();
        }
        /* BOOT: single / double / long press from the gesture machine, fed the real time
         * since the last poll (an IRQ wakes the task early, a busy I2C bus makes it late). */
        const TickType_t now = xTaskGetTickCount();
        const uint32_t elapsed_ms = (uint32_t)((now - last) * portTICK_PERIOD_MS);
        last = now;
        const bool down = gpio_get_level(PIN_BOOT_BUTTON) == 0;
        switch (button_gesture_update(&boot, down, elapsed_ms)) {
        case BUTTON_GESTURE_SINGLE:
            emit(BOARD_BUTTON_BOOT, BOARD_PRESS_SHORT);
            break;
        case BUTTON_GESTURE_DOUBLE:
            emit(BOARD_BUTTON_BOOT, BOARD_PRESS_DOUBLE);
            break;
        case BUTTON_GESTURE_LONG:
            emit(BOARD_BUTTON_BOOT, BOARD_PRESS_LONG);
            break;
        case BUTTON_GESTURE_NONE:
        default:
            break;
        }
    }
}

static esp_err_t buttons_init(void)
{
    /* gpio_config() also takes the PMU line back from RTC IO after a standby wake. */
    const gpio_config_t lines = {
        .pin_bit_mask = (1ULL << PIN_BOOT_BUTTON) | (1ULL << PIN_PMU_IRQ),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_LOW_LEVEL,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&lines), TAG, "button gpios");
    /* Masked until the task has started and finds the lines high. */
    gpio_intr_disable(PIN_BOOT_BUTTON);
    gpio_intr_disable(PIN_PMU_IRQ);
    ESP_RETURN_ON_ERROR(gpio_wakeup_enable(PIN_BOOT_BUTTON, GPIO_INTR_LOW_LEVEL), TAG, "boot wake");
    ESP_RETURN_ON_ERROR(gpio_wakeup_enable(PIN_PMU_IRQ, GPIO_INTR_LOW_LEVEL), TAG, "irq wake");
    ESP_RETURN_ON_ERROR(esp_sleep_enable_gpio_wakeup(), TAG, "gpio wake");
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(PIN_BOOT_BUTTON, line_isr,
                                             (void *)(intptr_t)PIN_BOOT_BUTTON), TAG, "boot isr");
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(PIN_PMU_IRQ, line_isr, (void *)(intptr_t)PIN_PMU_IRQ),
                        TAG, "irq isr");
    ESP_RETURN_ON_FALSE(xTaskCreate(button_task, "buttons", 4096, NULL, 6, &s_button_task) == pdPASS,
                        ESP_ERR_NO_MEM, TAG, "task");
    return ESP_OK;
}

void board_buttons_set_callback(board_button_cb_t cb, void *ctx)
{
    s_button_cb = cb;
    s_button_ctx = ctx;
}

bool board_button_is_down(board_button_t button)
{
    return button == BOARD_BUTTON_BOOT && gpio_get_level(PIN_BOOT_BUTTON) == 0;
}

bool board_screen_on_while_recording(void)
{
    return false; /* battery: the screen lights for a few seconds per action */
}

const char *board_button_hint(void)
{
    return "PWR: REC  2X BOOT: MUTE";
}

esp_err_t board_haptic_pulse(int count)
{
    ESP_RETURN_ON_FALSE(s_have_haptic, ESP_ERR_NOT_SUPPORTED, TAG, "no haptics");
    ESP_RETURN_ON_ERROR(xl9555_write(EXP_PIN_HAPTIC_EN, true), TAG, "en");
    vTaskDelay(pdMS_TO_TICKS(5));
    esp_err_t err = drv2605_init(I2C_ADDR_DRV2605); /* EN low drops the registers */
    for (int i = 0; i < count && err == ESP_OK; i++) {
        err = drv2605_play(1 /* strong click 100 % */, 500);
        if (i + 1 < count) {
            vTaskDelay(pdMS_TO_TICKS(150));
        }
    }
    drv2605_standby();
    xl9555_write(EXP_PIN_HAPTIC_EN, false);
    return err;
}

esp_err_t board_haptic_long(void)
{
    ESP_RETURN_ON_FALSE(s_have_haptic, ESP_ERR_NOT_SUPPORTED, TAG, "no haptics");
    ESP_RETURN_ON_ERROR(xl9555_write(EXP_PIN_HAPTIC_EN, true), TAG, "en");
    vTaskDelay(pdMS_TO_TICKS(5));
    esp_err_t err = drv2605_init(I2C_ADDR_DRV2605);
    /* Library effect 47 "Buzz 1 - 100 %", played back to back: one continuous long buzz. */
    for (int i = 0; i < 2 && err == ESP_OK; i++) {
        err = drv2605_play(47, 1000);
    }
    drv2605_standby();
    xl9555_write(EXP_PIN_HAPTIC_EN, false);
    return err;
}

/* -------------------------------------------------------------- mic/sd/ui ---- */

board_mic_pins_t board_mic_pins(void)
{
    return (board_mic_pins_t){.clk_gpio = PIN_MIC_CLK, .data_gpio = PIN_MIC_DATA};
}

int board_mic_eq(const board_mic_eq_band_t **bands)
{
    /* Measured 2026-10-19 (20 s of speech at ~30 cm, third-octave spectrum): the mic's port
     * in the case leaves a ~10 dB hole at 1-2.5 kHz and a ~10 dB peak at 3-4 kHz, which
     * sounds muffled. These two bands bring it close to a normal speech spectrum; the owner
     * picked this version in a listening test. Milestone 3 refines it by transcription. */
    static const board_mic_eq_band_t eq[] = {
        {.hz = 1600.0f, .gain_db = 9.0f, .q = 1.0f},
        {.hz = 3600.0f, .gain_db = -6.0f, .q = 2.0f},
    };
    *bands = eq;
    return (int)(sizeof(eq) / sizeof(eq[0]));
}

bool board_storage_card_present(void)
{
    bool high = true;
    if (!s_have_expander || xl9555_read(EXP_PIN_SD_DET, &high) != ESP_OK) {
        return false;
    }
    return !high; /* card-detect switch pulls low when a card is in */
}

const char *board_storage_label(void)
{
    return "SD";
}

uint64_t board_storage_reserve_bytes(void)
{
    return 8ull * 1024 * 1024;
}

/* The recorder and console commands can use the card at the same time: count the users and
 * power the card off only when the last one unmounts. */
static SemaphoreHandle_t s_storage_lock;
static StaticSemaphore_t s_storage_lock_buf;
static int s_storage_users;

esp_err_t board_storage_mount(const char **mount_point)
{
    xSemaphoreTake(s_storage_lock, portMAX_DELAY);
    esp_err_t err = ESP_OK;
    if (!sdcard_is_mounted()) {
        if (!board_storage_card_present()) {
            ESP_LOGW(TAG, "no card detected (SD_DET high); trying anyway in case the pin map is wrong");
        }
        err = board_rail_set(BOARD_RAIL_SD, true);
        if (err == ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(20));
            err = sdcard_mount();
            if (err != ESP_OK) {
                board_rail_set(BOARD_RAIL_SD, false);
            }
        }
    }
    if (err == ESP_OK) {
        s_storage_users++;
        *mount_point = SDCARD_MOUNT_POINT;
    }
    xSemaphoreGive(s_storage_lock);
    return err;
}

esp_err_t board_storage_unmount(void)
{
    xSemaphoreTake(s_storage_lock, portMAX_DELAY);
    esp_err_t err = ESP_OK;
    if (s_storage_users > 0 && --s_storage_users == 0) {
        sdcard_unmount();
        err = board_rail_set(BOARD_RAIL_SD, false);
    }
    xSemaphoreGive(s_storage_lock);
    return err;
}

#define SD_POWER_OFF_MS 100 /* card rail off before it comes back */

esp_err_t board_storage_power_cycle(void)
{
    xSemaphoreTake(s_storage_lock, portMAX_DELAY);
    if (s_storage_users == 0) {
        xSemaphoreGive(s_storage_lock);
        return ESP_ERR_INVALID_STATE;
    }
    const int64_t t0 = esp_timer_get_time();
    sdcard_unmount();
    board_rail_set(BOARD_RAIL_SD, false);
    vTaskDelay(pdMS_TO_TICKS(SD_POWER_OFF_MS));
    esp_err_t err = board_rail_set(BOARD_RAIL_SD, true);
    if (err == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(20));
        err = sdcard_mount();
    }
    if (err != ESP_OK) {
        board_rail_set(BOARD_RAIL_SD, false);
    }
    xSemaphoreGive(s_storage_lock);
    ESP_LOGW(TAG, "SD card powered off and on: %s (%lld ms)", esp_err_to_name(err),
             (long long)((esp_timer_get_time() - t0) / 1000));
    return err;
}

bool board_storage_is_mounted(void)
{
    return sdcard_is_mounted();
}

esp_err_t board_storage_info(uint64_t *total_bytes, uint64_t *free_bytes)
{
    return sdcard_info(total_bytes, free_bytes);
}

esp_err_t board_display_on(board_display_info_t *info)
{
    ESP_RETURN_ON_FALSE(s_have_expander, ESP_ERR_INVALID_STATE, TAG, "no expander");
    ESP_RETURN_ON_ERROR(board_rail_set(BOARD_RAIL_DISPLAY, true), TAG, "rail");
    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_RETURN_ON_ERROR(xl9555_write(EXP_PIN_DISP_EN, true), TAG, "en");
    vTaskDelay(pdMS_TO_TICKS(20));
    esp_err_t err = display_init();
    if (err != ESP_OK) {
        board_display_off();
        return err;
    }
    info->width = DISP_WIDTH;
    info->height = DISP_HEIGHT;
    return ESP_OK;
}

esp_err_t board_display_draw(int x, int y, int w, int h, const uint16_t *pixels)
{
    return display_draw(x, y, w, h, pixels);
}

esp_err_t board_display_set_brightness(int percent)
{
    return display_set_brightness(percent);
}

esp_err_t board_display_off(void)
{
    display_deinit();
    xl9555_write(EXP_PIN_DISP_EN, false);
    return board_rail_set(BOARD_RAIL_DISPLAY, false);
}

/* ----------------------------------------------------------------- init ---- */

esp_err_t board_init(void)
{
    s_storage_lock = xSemaphoreCreateMutexStatic(&s_storage_lock_buf);
    ESP_LOGI(TAG, "T-Watch Ultra bring-up");
    ESP_RETURN_ON_ERROR(i2c_bus_init(PIN_I2C_SDA, PIN_I2C_SCL), TAG, "i2c");

    const struct { uint8_t addr; const char *name; } chips[] = {
        {I2C_ADDR_AXP2101, "AXP2101 PMU"},   {I2C_ADDR_XL9555, "XL9555 expander"},
        {I2C_ADDR_PCF85063A, "PCF85063A RTC"}, {I2C_ADDR_DRV2605, "DRV2605 haptics (EN low)"},
        {I2C_ADDR_CST9217, "CST9217 touch (rail off)"}, {I2C_ADDR_BHI260AP, "BHI260AP sensor (rail off)"},
    };
    for (unsigned i = 0; i < sizeof(chips) / sizeof(chips[0]); i++) {
        ESP_LOGI(TAG, "i2c 0x%02X %-28s %s", chips[i].addr, chips[i].name,
                 i2c_bus_probe(chips[i].addr) ? "present" : "absent");
    }

    ESP_RETURN_ON_ERROR(pmu_init(), TAG, "pmu");

    /* Shared SPI bus: chip selects of the unpowered LoRa and NFC chips stay Hi-Z (plan §2.2). */
    hiz_pin(PIN_LORA_CS);
    hiz_pin(PIN_LORA_RST);
    hiz_pin(PIN_NFC_CS);
    hiz_pin(PIN_SD_CS);
    ESP_LOGI(TAG, "LoRa CS/RST (GPIO%d/%d) and NFC CS (GPIO%d) left as inputs", PIN_LORA_CS,
             PIN_LORA_RST, PIN_NFC_CS);

    if (expander_init() != ESP_OK) {
        ESP_LOGE(TAG, "XL9555 not found: no haptics, display or card detect");
    }

    if (pcf85063a_init(I2C_ADDR_PCF85063A) == ESP_OK) {
        s_have_rtc = true;
        int64_t epoch = 0;
        bool valid = false;
        char buf[24];
        board_rtc_get(&epoch, &valid);
        isotime_format(epoch, buf, sizeof(buf));
        ESP_LOGI(TAG, "RTC reads %s (%s)", buf, valid ? "valid" : "not set or lost power");
    } else {
        ESP_LOGE(TAG, "PCF85063A not found");
    }

    if (s_have_expander) {
        xl9555_write(EXP_PIN_HAPTIC_EN, true);
        vTaskDelay(pdMS_TO_TICKS(5));
        if (drv2605_init(I2C_ADDR_DRV2605) == ESP_OK) {
            s_have_haptic = true;
            drv2605_standby();
        } else {
            ESP_LOGE(TAG, "DRV2605 not found");
        }
        xl9555_write(EXP_PIN_HAPTIC_EN, false);
    }

    ESP_RETURN_ON_ERROR(buttons_init(), TAG, "buttons");

    board_log_power();
    board_log_expander();
    return ESP_OK;
}
