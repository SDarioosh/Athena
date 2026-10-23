/* LilyGO T-Watch S3 board layer: implements main/board.h.
 *
 * Differences from the T-Watch Ultra:
 * - One button: the side key, the AXP2101's PWRON. Clicks are counted (click_counter.h) and
 *   reported as the Ultra's buttons, so the app's actions stay the same:
 *     1 click  -> PWR short   start / stop a recording session
 *     2 clicks -> BOOT double mute / unmute
 *     3 clicks -> BOOT short  status screen
 *     4 clicks -> BOOT long   BLE sync window (pair a phone)
 *     hold ~1 s -> PWR long   voice note
 *     hold ~6 s               hardware power-off (AXP2101)
 * - No SD card: storage is a FAT partition in flash (flashfs.c), mounted at /flash.
 * - No I/O expander: haptics enable and the display power are AXP2101 rails (pins.h).
 * - PCF8563 RTC instead of the PCF85063A; ST7789 240x240 LCD instead of the AMOLED. */
#include "board.h"

#include <inttypes.h>
#include <string.h>

#include "axp2101.h"
#include "click_counter.h"
#include "display.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "drv2605.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "flashfs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "isotime.h"
#include "pcf8563.h"
#include "pins.h"

static const char *TAG = "board";

static board_button_cb_t s_button_cb;
static void *s_button_ctx;
static TaskHandle_t s_button_task;
static bool s_have_pmu, s_have_rtc, s_have_haptic;

#define BUTTON_POLL_MS 10       /* only while the PMU IRQ line is low */
#define PWR_LONG_PRESS_MS 1000  /* PKEY_LONG IRQ time (AXP2101 reg 0x27 bits[5:4]) */
#define PWR_OFF_HOLD_MS 6000    /* hardware power-off hold (bits[3:2]), as the Ultra ships */

#define PMU_IRQ_MASK (AXP_IRQ_PKEY_SHORT | AXP_IRQ_PKEY_LONG | AXP_IRQ_VBUS_INSERT | \
                      AXP_IRQ_VBUS_REMOVE | AXP_IRQ_CHG_START | AXP_IRQ_CHG_DONE)
#define PMU_WAKE_MASK (AXP_IRQ_PKEY_SHORT | AXP_IRQ_PKEY_LONG | AXP_IRQ_VBUS_INSERT)

/* A board rail and, for some, a second AXP2101 rail switched with it. AXP_RAIL_COUNT = none. */
static const struct {
    axp_rail_t rail;
    axp_rail_t also;
    const char *what;
} s_rails[BOARD_RAIL_COUNT] = {
    [BOARD_RAIL_SD] = {AXP_RAIL_COUNT, AXP_RAIL_COUNT, "SD card (none)"},
    [BOARD_RAIL_DISPLAY] = {AXP_ALDO3, AXP_ALDO2, "display"}, /* logic + backlight */
    [BOARD_RAIL_GPS] = {AXP_BLDO1, AXP_DC3, "GPS"},           /* newer + older versions */
    [BOARD_RAIL_LORA] = {AXP_ALDO4, AXP_RAIL_COUNT, "LoRa"},
    [BOARD_RAIL_NFC] = {AXP_RAIL_COUNT, AXP_RAIL_COUNT, "NFC (none)"},
    [BOARD_RAIL_SPEAKER] = {AXP_DLDO1, AXP_RAIL_COUNT, "speaker"},
    [BOARD_RAIL_SENSOR] = {AXP_RAIL_COUNT, AXP_RAIL_COUNT, "sensor (on 3V3)"},
};

const char *board_name(void)
{
    return "twatch_s3";
}

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ---------------------------------------------------------------- power ---- */

static void hiz_pin(int gpio)
{
    gpio_reset_pin(gpio);
    gpio_set_direction(gpio, GPIO_MODE_INPUT);
    gpio_set_pull_mode(gpio, GPIO_FLOATING);
}

static esp_err_t pmu_init(void)
{
    ESP_RETURN_ON_ERROR(axp2101_init(I2C_ADDR_AXP2101), TAG, "axp2101");
    s_have_pmu = true;

    /* DC1 feeds the ESP32-S3: never touch it except to confirm it is on. */
    if (!axp2101_rail_is_enabled(AXP_DC1)) {
        ESP_LOGE(TAG, "DC1 reads as off while we run on it; PMU read is suspect");
    }
    /* Voltages for the rails we switch (LilyGoLib: all 3.3 V). */
    axp2101_rail_set_mv(AXP_ALDO1, 3300);
    axp2101_rail_set_mv(AXP_ALDO2, 3300);
    axp2101_rail_set_mv(AXP_ALDO3, 3300);
    axp2101_rail_set_mv(AXP_BLDO2, 3300);

    /* The RTC rail stays on (tiny load; LilyGoLib: "don't change"). Everything else off
     * except DC1; the factory firmware leaves most of them on. */
    axp2101_rail_enable(AXP_ALDO1, true);
    const axp_rail_t off[] = {AXP_DC2,   AXP_DC3,   AXP_DC4,   AXP_DC5,   AXP_CPUSLDO,
                              AXP_ALDO2, AXP_ALDO3, AXP_ALDO4, AXP_BLDO1, AXP_BLDO2,
                              AXP_DLDO1, AXP_DLDO2};
    for (unsigned i = 0; i < sizeof(off) / sizeof(off[0]); i++) {
        axp2101_rail_enable(off[i], false);
    }

    axp2101_set_charge_current_ma(BOARD_CHARGE_CURRENT_MA);
    axp2101_set_charge_voltage_4v2();
    axp2101_chgled_off();
    axp2101_backup_charge_enable(3300);
    axp2101_battery_detect_enable();
    /* VBAT only. This board has no battery thermistor: the TS pin must not feed the charger
     * (LilyGoLib: "otherwise it will cause abnormal charging"). */
    axp2101_adc_enable(0x01);
    esp_err_t terr = axp2101_ts_pin_disable();
    if (terr != ESP_OK) {
        ESP_LOGW(TAG, "TS pin not disabled: %s", esp_err_to_name(terr));
    }
    /* The AXP2101's default battery model; LilyGO ships no model for this cell. */

    esp_err_t kerr = axp2101_set_pkey_long_ms(PWR_LONG_PRESS_MS);
    esp_err_t oerr = axp2101_set_pkey_off_ms(PWR_OFF_HOLD_MS);
    int long_ms = -1, off_ms = -1, on_ms = -1;
    axp2101_get_pkey_times(&long_ms, &off_ms, &on_ms);
    ESP_LOGI(TAG, "PWR key: long press %d ms (%s), power-off hold %d ms (%s), power-on %d ms",
             long_ms, esp_err_to_name(kerr), off_ms, esp_err_to_name(oerr), on_ms);

    axp2101_irq_enable(PMU_IRQ_MASK);
    uint32_t pending;
    axp2101_irq_read_clear(&pending);
    return ESP_OK;
}

esp_err_t board_rail_set(board_rail_t rail, bool on)
{
    ESP_RETURN_ON_FALSE(rail < BOARD_RAIL_COUNT, ESP_ERR_INVALID_ARG, TAG, "rail");
    ESP_RETURN_ON_FALSE(s_rails[rail].rail != AXP_RAIL_COUNT, ESP_ERR_NOT_SUPPORTED, TAG, "no rail");
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_INVALID_STATE, TAG, "no pmu");
    ESP_RETURN_ON_ERROR(axp2101_rail_enable(s_rails[rail].rail, on), TAG, "set");
    if (s_rails[rail].also != AXP_RAIL_COUNT) {
        ESP_RETURN_ON_ERROR(axp2101_rail_enable(s_rails[rail].also, on), TAG, "set");
    }
    ESP_LOGD(TAG, "rail %s (%s) -> %s", axp2101_rail_name(s_rails[rail].rail), s_rails[rail].what,
             on ? "on" : "off");
    return ESP_OK;
}

bool board_rail_is_on(board_rail_t rail)
{
    return rail < BOARD_RAIL_COUNT && s_rails[rail].rail != AXP_RAIL_COUNT && s_have_pmu &&
           axp2101_rail_is_enabled(s_rails[rail].rail);
}

void board_log_power(void)
{
    if (!s_have_pmu) {
        ESP_LOGW(TAG, "no PMU");
        return;
    }
    ESP_LOGI(TAG, "power rails (read back from the AXP2101):");
    for (board_rail_t r = 0; r < BOARD_RAIL_COUNT; r++) {
        if (s_rails[r].rail == AXP_RAIL_COUNT) {
            ESP_LOGI(TAG, "  %-16s -", s_rails[r].what);
        } else {
            ESP_LOGI(TAG, "  %-16s %-7s %s", s_rails[r].what, axp2101_rail_name(s_rails[r].rail),
                     axp2101_rail_is_enabled(s_rails[r].rail) ? "ON" : "off");
        }
    }
    ESP_LOGI(TAG, "  RTC (ALDO1) %s, haptics enable (BLDO2) %s",
             axp2101_rail_is_enabled(AXP_ALDO1) ? "ON" : "off",
             axp2101_rail_is_enabled(AXP_BLDO2) ? "ON" : "off");
    ESP_LOGI(TAG, "  charge current %d mA (limit %d mA)", axp2101_get_charge_current_ma(),
             BOARD_CHARGE_CURRENT_MA);
    axp2101_log_registers();
}

esp_err_t board_standby_prepare(void)
{
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_NOT_SUPPORTED, TAG, "no pmu");
    /* Only the PMU's IRQ line wakes the chip: a PWR press or USB power. */
    ESP_RETURN_ON_ERROR(axp2101_irq_enable(PMU_WAKE_MASK), TAG, "irq mask");
    uint32_t pending = 0;
    axp2101_irq_read_clear(&pending);
    if (gpio_get_level(PIN_PMU_IRQ) == 0) {
        axp2101_irq_enable(PMU_IRQ_MASK);
        ESP_LOGW(TAG, "standby: PMU IRQ line still low");
        return ESP_ERR_INVALID_STATE;
    }
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO); /* the light-sleep button line */
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
    /* Same limits as the Ultra: 5 % or 3.5 V stops recording, 10 % or USB allows it again. */
    *out = (bg_limits_t){.low_percent = 5, .low_mv = 3500, .resume_percent = 10};
}

/* ------------------------------------------------------------- expander ---- */

void board_log_expander(void)
{
    /* No I/O expander on this watch: show the lines that matter instead. */
    ESP_LOGI(TAG, "no I/O expander; PMU IRQ (GPIO%d) %s, RTC INT (GPIO%d) %s", PIN_PMU_IRQ,
             gpio_get_level(PIN_PMU_IRQ) ? "high" : "LOW", PIN_RTC_IRQ,
             gpio_get_level(PIN_RTC_IRQ) ? "high" : "LOW");
}

/* ------------------------------------------------------------------ rtc ---- */

esp_err_t board_rtc_get(int64_t *epoch, bool *valid)
{
    ESP_RETURN_ON_FALSE(s_have_rtc, ESP_ERR_INVALID_STATE, TAG, "no rtc");
    pcf8563_datetime_t dt;
    bool low = true;
    ESP_RETURN_ON_ERROR(pcf8563_read(&dt, &low), TAG, "read");
    const isotime_fields_t f = {dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second};
    int64_t e = 0;
    const bool ok = isotime_from_fields(&f, &e);
    *epoch = ok ? e : 0;
    *valid = ok && !low && isotime_is_plausible(e);
    return ESP_OK;
}

esp_err_t board_rtc_set(int64_t epoch)
{
    ESP_RETURN_ON_FALSE(s_have_rtc, ESP_ERR_INVALID_STATE, TAG, "no rtc");
    isotime_fields_t f;
    isotime_to_fields(epoch, &f);
    const pcf8563_datetime_t dt = {f.year, f.month, f.day, f.hour, f.minute, f.second};
    return pcf8563_write(&dt);
}

/* -------------------------------------------------------- buttons/haptic ---- */

/* The PMU IRQ line is a level-low interrupt (and a light-sleep wake source). The ISR masks
 * it and wakes the task, which reads the PMU until the line is high again and unmasks it. */
static void IRAM_ATTR line_isr(void *arg)
{
    gpio_intr_disable((gpio_num_t)(intptr_t)arg);
    BaseType_t woken = pdFALSE;
    if (s_button_task) {
        vTaskNotifyGiveFromISR(s_button_task, &woken);
    }
    portYIELD_FROM_ISR(woken);
}

static void emit(board_button_t b, board_press_t p, const char *what)
{
    ESP_LOGI(TAG, "button: %s", what);
    if (s_button_cb) {
        s_button_cb(b, p, s_button_ctx);
    }
}

static void emit_clicks(int n)
{
    switch (n) {
    case 1: emit(BOARD_BUTTON_PWR, BOARD_PRESS_SHORT, "1 click (session start/stop)"); break;
    case 2: emit(BOARD_BUTTON_BOOT, BOARD_PRESS_DOUBLE, "2 clicks (mute)"); break;
    case 3: emit(BOARD_BUTTON_BOOT, BOARD_PRESS_SHORT, "3 clicks (status)"); break;
    case 4: emit(BOARD_BUTTON_BOOT, BOARD_PRESS_LONG, "4 clicks (sync window)"); break;
    default: ESP_LOGI(TAG, "button: %d clicks, ignored", n); break;
    }
}

static void act(click_action_t a, int clicks)
{
    switch (a) {
    case CLICK_ACTION_CLICKS: emit_clicks(clicks); break;
    case CLICK_ACTION_HOLD: emit(BOARD_BUTTON_PWR, BOARD_PRESS_LONG, "hold (voice note)"); break;
    case CLICK_ACTION_IGNORED: ESP_LOGI(TAG, "button: click ignored (within 1 s of the last action)"); break;
    case CLICK_ACTION_NONE:
    default: break;
    }
}

static void handle_pmu_irq(click_counter_t *cc)
{
    uint32_t st = 0;
    if (axp2101_irq_read_clear(&st) != ESP_OK || st == 0) {
        return;
    }
    int clicks = 0;
    if (st & AXP_IRQ_PKEY_SHORT) {
        act(click_counter_short(cc, now_ms(), &clicks), clicks);
    }
    if (st & AXP_IRQ_PKEY_LONG) {
        act(click_counter_long(cc, now_ms()), 0);
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
    const uint32_t other = st & ~(uint32_t)PMU_IRQ_MASK;
    if (other) {
        ESP_LOGI(TAG, "PMU irq status 0x%06" PRIx32, other);
    }
}

static void button_task(void *arg)
{
    (void)arg;
    click_counter_t cc;
    click_counter_init(&cc);
    for (;;) {
        /* Line high: unmask it and sleep until it goes low or a click count is due (the chip
         * can light sleep meanwhile). Line low: poll until the PMU has let go of it. */
        const bool low = gpio_get_level(PIN_PMU_IRQ) == 0;
        TickType_t wait = portMAX_DELAY;
        if (low) {
            wait = pdMS_TO_TICKS(BUTTON_POLL_MS);
        } else {
            gpio_intr_enable(PIN_PMU_IRQ);
            const uint32_t due = click_counter_wait_ms(&cc, now_ms());
            if (due != UINT32_MAX) {
                wait = pdMS_TO_TICKS(due) + 1;
            }
        }
        ulTaskNotifyTake(pdTRUE, wait);
        if (gpio_get_level(PIN_PMU_IRQ) == 0) {
            handle_pmu_irq(&cc);
        }
        int clicks = 0;
        act(click_counter_poll(&cc, now_ms(), &clicks), clicks);
    }
}

static esp_err_t buttons_init(void)
{
    /* gpio_config() also takes the line back from RTC IO after a standby wake. */
    const gpio_config_t line = {
        .pin_bit_mask = 1ULL << PIN_PMU_IRQ,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_LOW_LEVEL,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&line), TAG, "irq gpio");
    gpio_intr_disable(PIN_PMU_IRQ); /* masked until the task finds the line high */
    ESP_RETURN_ON_ERROR(gpio_wakeup_enable(PIN_PMU_IRQ, GPIO_INTR_LOW_LEVEL), TAG, "irq wake");
    ESP_RETURN_ON_ERROR(esp_sleep_enable_gpio_wakeup(), TAG, "gpio wake");
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(PIN_PMU_IRQ, line_isr, (void *)(intptr_t)PIN_PMU_IRQ),
                        TAG, "irq isr");
    ESP_RETURN_ON_FALSE(xTaskCreate(button_task, "buttons", 4096, NULL, 6, &s_button_task) == pdPASS,
                        ESP_ERR_NO_MEM, TAG, "task");
    return ESP_OK;
}

/* No PMIC button interrupts on this board: nothing to report. */
void board_pwr_event_info(uint32_t *pmu_irq, int32_t *since_prev_ms)
{
    *pmu_irq = 0;
    *since_prev_ms = 0;
}

void board_buttons_set_callback(board_button_cb_t cb, void *ctx)
{
    s_button_cb = cb;
    s_button_ctx = ctx;
}

bool board_button_is_down(board_button_t button)
{
    (void)button;
    return false; /* the PMU key reports events, not a level */
}

bool board_screen_on_while_recording(void)
{
    return false; /* battery: the screen lights for a few seconds per action */
}

const char *board_button_hint(void)
{
    return "1:REC 2:MUTE 3:INFO";
}

/* BLDO2 is the DRV2605's EN pin. */
static esp_err_t haptic_begin(void)
{
    ESP_RETURN_ON_ERROR(axp2101_rail_enable(AXP_BLDO2, true), TAG, "en");
    vTaskDelay(pdMS_TO_TICKS(5));
    return drv2605_init(I2C_ADDR_DRV2605); /* EN low drops the registers */
}

static void haptic_end(void)
{
    drv2605_standby();
    axp2101_rail_enable(AXP_BLDO2, false);
}

esp_err_t board_haptic_pulse(int count)
{
    ESP_RETURN_ON_FALSE(s_have_haptic, ESP_ERR_NOT_SUPPORTED, TAG, "no haptics");
    esp_err_t err = haptic_begin();
    for (int i = 0; i < count && err == ESP_OK; i++) {
        err = drv2605_play(1 /* strong click 100 % */, 500);
        if (i + 1 < count) {
            vTaskDelay(pdMS_TO_TICKS(150));
        }
    }
    haptic_end();
    return err;
}

esp_err_t board_haptic_long(void)
{
    ESP_RETURN_ON_FALSE(s_have_haptic, ESP_ERR_NOT_SUPPORTED, TAG, "no haptics");
    esp_err_t err = haptic_begin();
    /* Library effect 47 "Buzz 1 - 100 %", played back to back: one continuous long buzz. */
    for (int i = 0; i < 2 && err == ESP_OK; i++) {
        err = drv2605_play(47, 1000);
    }
    haptic_end();
    return err;
}

/* -------------------------------------------------------------- mic/storage ---- */

board_mic_pins_t board_mic_pins(void)
{
    return (board_mic_pins_t){.clk_gpio = PIN_MIC_CLK, .data_gpio = PIN_MIC_DATA};
}

int board_mic_eq(const board_mic_eq_band_t **bands)
{
    /* Not measured on this watch yet: flat. */
    *bands = NULL;
    return 0;
}

bool board_storage_card_present(void)
{
    return true;
}

const char *board_storage_label(void)
{
    return "FLASH";
}

uint64_t board_storage_reserve_bytes(void)
{
    /* The partition is ~11.8 MB; 1 MB covers FAT, the power log and a last chunk. */
    return 1024ull * 1024;
}

/* The recorder and console commands can use storage at the same time: count the users and
 * unmount when the last one is done (standby needs it unmounted). */
static SemaphoreHandle_t s_storage_lock;
static StaticSemaphore_t s_storage_lock_buf;
static int s_storage_users;

esp_err_t board_storage_mount(const char **mount_point)
{
    xSemaphoreTake(s_storage_lock, portMAX_DELAY);
    const esp_err_t err = flashfs_mount();
    if (err == ESP_OK) {
        s_storage_users++;
        *mount_point = FLASHFS_MOUNT_POINT;
    }
    xSemaphoreGive(s_storage_lock);
    return err;
}

esp_err_t board_storage_unmount(void)
{
    xSemaphoreTake(s_storage_lock, portMAX_DELAY);
    esp_err_t err = ESP_OK;
    if (s_storage_users > 0 && --s_storage_users == 0) {
        err = flashfs_unmount();
    }
    xSemaphoreGive(s_storage_lock);
    return err;
}

/* Built-in flash: nothing to power off. */
esp_err_t board_storage_power_cycle(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

bool board_storage_is_mounted(void)
{
    return flashfs_is_mounted();
}

esp_err_t board_storage_info(uint64_t *total_bytes, uint64_t *free_bytes)
{
    return flashfs_info(total_bytes, free_bytes);
}

/* ---------------------------------------------------------------- display ---- */

esp_err_t board_display_on(board_display_info_t *info)
{
    ESP_RETURN_ON_FALSE(s_have_pmu, ESP_ERR_INVALID_STATE, TAG, "no pmu");
    ESP_RETURN_ON_ERROR(board_rail_set(BOARD_RAIL_DISPLAY, true), TAG, "rail");
    /* The panel's RST is an RC on its supply: give it time to come out of reset. */
    vTaskDelay(pdMS_TO_TICKS(30));
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
    display_deinit(); /* pins Hi-Z before the rails go off */
    return board_rail_set(BOARD_RAIL_DISPLAY, false);
}

/* ----------------------------------------------------------------- init ---- */

esp_err_t board_init(void)
{
    s_storage_lock = xSemaphoreCreateMutexStatic(&s_storage_lock_buf);
    ESP_LOGI(TAG, "T-Watch S3 bring-up");
    ESP_RETURN_ON_ERROR(i2c_bus_init(PIN_I2C_SDA, PIN_I2C_SCL), TAG, "i2c");

    ESP_RETURN_ON_ERROR(pmu_init(), TAG, "pmu");

    const struct { uint8_t addr; const char *name; } chips[] = {
        {I2C_ADDR_AXP2101, "AXP2101 PMU"},
        {I2C_ADDR_PCF8563, "PCF8563 RTC"},
        {I2C_ADDR_DRV2605, "DRV2605 haptics (EN low)"},
        {I2C_ADDR_BMA423, "BMA423 accelerometer (unused)"},
    };
    for (unsigned i = 0; i < sizeof(chips) / sizeof(chips[0]); i++) {
        ESP_LOGI(TAG, "i2c 0x%02X %-30s %s", chips[i].addr, chips[i].name,
                 i2c_bus_probe(chips[i].addr) ? "present" : "absent");
    }

    /* LoRa rail is off: keep its chip select and reset Hi-Z. */
    hiz_pin(PIN_LORA_CS);
    hiz_pin(PIN_LORA_RST);

    if (pcf8563_init(I2C_ADDR_PCF8563) == ESP_OK) {
        s_have_rtc = true;
        int64_t epoch = 0;
        bool valid = false;
        char buf[24];
        board_rtc_get(&epoch, &valid);
        isotime_format(epoch, buf, sizeof(buf));
        ESP_LOGI(TAG, "RTC reads %s (%s)", buf, valid ? "valid" : "not set or lost power");
    } else {
        ESP_LOGE(TAG, "PCF8563 not found");
    }

    if (haptic_begin() == ESP_OK) {
        s_have_haptic = true;
    } else {
        ESP_LOGE(TAG, "DRV2605 not found");
    }
    haptic_end();

    ESP_RETURN_ON_ERROR(buttons_init(), TAG, "buttons");

    board_log_power();
    board_log_expander();
    return ESP_OK;
}
