/* LilyGO T4-S3 board layer: a table-top conference recorder with four INMP441 mics.
 * Implements main/board.h.
 *
 * Differences from the T-Watch boards:
 * - One button: BOOT (GPIO0). Its presses go through the same click counter as the
 *   T-Watch S3's side key (key_press.h -> click_counter.h), reported as the Ultra's buttons
 *   so the app's actions stay the same:
 *     1 click   -> PWR short    start / stop a recording session
 *     2 clicks  -> BOOT double  mute / unmute
 *     3 clicks  -> BOOT short   status screen
 *     4 clicks  -> BOOT long    BLE sync window (pair a phone)
 *     hold ~1 s -> PWR long     voice note; a click or another hold ends it
 *     tap the screen -> BOOT short  status screen (the touch chip's INT line)
 *   The board's slide switch is the power switch.
 * - Mics: four INMP441 on I2S (pins.h), mixed to mono in main/mic.c.
 * - No haptics: board_haptic_*() return ESP_ERR_NOT_SUPPORTED; every action still shows on
 *   the screen.
 * - No RTC chip: wall-clock time is the ESP32-S3's own clock. It runs through light and
 *   deep sleep and survives a reset, but not the power switch; the phone sets it at every
 *   sync (or `time` on the console).
 * - Charger: SY6970 (no fuel gauge: the percentage is estimated from the voltage).
 * - Storage: a FAT partition in flash (boards/common/flashfs.c), as the T-Watch S3. The
 *   microSD slot isn't used yet. */
#include "board.h"

#include <inttypes.h>
#include <string.h>
#include <sys/time.h>

#include "click_counter.h"
#include "display.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_attr.h"
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
#include "key_press.h"
#include "pins.h"
#include "sy6970.h"

static const char *TAG = "board";

static board_button_cb_t s_button_cb;
static void *s_button_ctx;
static TaskHandle_t s_button_task;
static bool s_have_charger;
static bool s_have_touch;
static i2c_dev_t s_touch;

#define BUTTON_POLL_MS 10 /* only while the button is down or settling */
/* Button-task notification bits */
#define NOTE_BUTTON 0x01
#define NOTE_TOUCH 0x02
/* One tap = one status screen: while a finger stays on the glass the chip reports every
 * ~10 ms, so after a tap the touch line is ignored for this long. */
#define TOUCH_QUIET_MS 1500
#define TOUCH_POLL_MS 50 /* while a finger stays on the glass */
#define TOUCH_REPORT_LEN 28

const char *board_name(void)
{
    return "t4_s3";
}

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void hiz_pin(int gpio)
{
    gpio_reset_pin(gpio);
    gpio_set_direction(gpio, GPIO_MODE_INPUT);
    gpio_set_pull_mode(gpio, GPIO_FLOATING);
}

/* ---------------------------------------------------------------- power ---- */

/* The only switchable supply is the AMOLED's power IC. */
static bool s_display_power;

esp_err_t board_rail_set(board_rail_t rail, bool on)
{
    ESP_RETURN_ON_FALSE(rail < BOARD_RAIL_COUNT, ESP_ERR_INVALID_ARG, TAG, "rail");
    ESP_RETURN_ON_FALSE(rail == BOARD_RAIL_DISPLAY, ESP_ERR_NOT_SUPPORTED, TAG, "no rail");
    ESP_RETURN_ON_ERROR(gpio_set_level(PIN_DISP_EN, on ? 1 : 0), TAG, "en");
    s_display_power = on;
    return ESP_OK;
}

bool board_rail_is_on(board_rail_t rail)
{
    /* An output pin reads back its driven level when its input is enabled. */
    return rail == BOARD_RAIL_DISPLAY && gpio_get_level(PIN_DISP_EN) == 1;
}

void board_log_power(void)
{
    ESP_LOGI(TAG, "power: battery slide switch; display power IC (GPIO%d) %s", PIN_DISP_EN,
             board_rail_is_on(BOARD_RAIL_DISPLAY) ? "ON" : "off");
    if (!s_have_charger) {
        ESP_LOGW(TAG, "no SY6970 charger");
        return;
    }
    sy6970_reading_t r;
    if (sy6970_read(&r) == ESP_OK) {
        static const char *const CHG[] = {"not charging", "pre-charge", "fast charge", "done"};
        ESP_LOGI(TAG, "  battery %d mV, system %d mV, USB %d mV (%s), %s at %d mA (limit %d mA), "
                      "fault 0x%02X",
                 r.batv_mv, r.sysv_mv, r.vbus_mv, r.vbus_good ? "good" : "none",
                 CHG[r.status.chg & 3], r.ichg_now_ma, sy6970_charge_current_ma(), r.fault);
    }
    sy6970_log_registers();
}

esp_err_t board_standby_prepare(void)
{
    /* BOOT wakes the chip (USB power doesn't: the SY6970's INT is a 256 us pulse). */
    if (gpio_get_level(PIN_BUTTON) == 0) {
        ESP_LOGW(TAG, "standby: BOOT is held");
        return ESP_ERR_INVALID_STATE;
    }
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO); /* the light-sleep button line */
    rtc_gpio_pullup_en(PIN_BUTTON);
    rtc_gpio_pulldown_dis(PIN_BUTTON);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    return esp_sleep_enable_ext1_wakeup_io(1ULL << PIN_BUTTON, ESP_EXT1_WAKEUP_ANY_LOW);
}

esp_err_t board_battery_read(board_battery_t *out)
{
    ESP_RETURN_ON_FALSE(s_have_charger, ESP_ERR_INVALID_STATE, TAG, "no charger");
    sy6970_reading_t r;
    ESP_RETURN_ON_ERROR(sy6970_read(&r), TAG, "read");
    /* No battery at the charger (none plugged in, or the slide switch off, which cuts it):
     * the battery pin swings between ~3.5 and ~4.25 V while the charger keeps restarting,
     * "done"/"fast charge" at 0 mA, with BAT_FAULT set on every read (owner's board,
     * 2026-10-21). Switch on: a steady 4044 mV, 500 mA, no fault. */
    out->present = r.batv_mv >= 2500 && !(r.fault & SY6970_FAULT_BAT);
    out->millivolts = r.batv_mv;
    out->percent = out->present ? lipo_percent_from_mv(r.batv_mv) : -1;
    out->charging = r.status.chg == SY6970_CHG_PRE || r.status.chg == SY6970_CHG_FAST;
    out->vbus = r.vbus_good || r.status.power_good;
    return ESP_OK;
}

void board_battery_limits(bg_limits_t *out)
{
    /* The percentage is a voltage estimate: stop at 3.5 V (~3 %), allow again at 10 %. */
    *out = (bg_limits_t){.low_percent = 3, .low_mv = 3500, .resume_percent = 10};
}

void board_log_expander(void)
{
    ESP_LOGI(TAG, "no I/O expander; BOOT (GPIO%d) %s, charger INT (GPIO%d) %s, touch INT (GPIO%d) %s",
             PIN_BUTTON, gpio_get_level(PIN_BUTTON) ? "up" : "DOWN", PIN_PMU_IRQ,
             gpio_get_level(PIN_PMU_IRQ) ? "high" : "LOW", PIN_TOUCH_IRQ,
             gpio_get_level(PIN_TOUCH_IRQ) ? "high" : "LOW");
    if (s_have_touch) {
        uint8_t r[8] = {0};
        const esp_err_t err = i2c_reg_read(&s_touch, 0x00, r, sizeof(r));
        ESP_LOGI(TAG, "touch report (%s): %02X %02X %02X %02X %02X %02X %02X %02X",
                 esp_err_to_name(err), r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]);
    }
}

/* ------------------------------------------------------------------ rtc ---- */

/* No RTC chip. The system clock keeps running through sleep and resets; this marker in
 * RTC memory (not cleared by a reset, random after power-up) says it has been set. */
#define TIME_SET_MAGIC 0x54345333u /* "T4S3" */
static RTC_NOINIT_ATTR uint32_t s_time_set_magic;

esp_err_t board_rtc_get(int64_t *epoch, bool *valid)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    *epoch = tv.tv_sec;
    *valid = s_time_set_magic == TIME_SET_MAGIC && isotime_is_plausible(tv.tv_sec);
    return ESP_OK;
}

esp_err_t board_rtc_set(int64_t epoch)
{
    const struct timeval tv = {.tv_sec = (time_t)epoch, .tv_usec = 0};
    settimeofday(&tv, NULL);
    s_time_set_magic = TIME_SET_MAGIC;
    return ESP_OK;
}

/* -------------------------------------------------------------- buttons ---- */

/* Both lines are level-low interrupts (and light-sleep wake sources). The ISR masks its line
 * and wakes the task, which arms it again once it is high. arg = (pin << 8) | bit. */
static void IRAM_ATTR line_isr(void *arg)
{
    const uint32_t v = (uint32_t)(uintptr_t)arg;
    gpio_intr_disable((gpio_num_t)(v >> 8));
    BaseType_t woken = pdFALSE;
    if (s_button_task) {
        xTaskNotifyFromISR(s_button_task, v & 0xFF, eSetBits, &woken);
    }
    portYIELD_FROM_ISR(woken);
}

/* ------------------------------------------------------------------ touch ---- */

/* CST226SE (I2C 0x5A): a touch report is 28 bytes from register 0x00; byte 6 is 0xAB when it
 * is valid and byte 5 holds the number of points (SensorLib TouchDrvCST226, MIT). Reading it
 * lets the chip release INT; no acknowledgement is needed. Coordinates aren't used: any touch
 * is a tap. */
static bool touch_read_tap(void)
{
    uint8_t r[TOUCH_REPORT_LEN];
    if (i2c_reg_read(&s_touch, 0x00, r, sizeof(r)) != ESP_OK) {
        return false;
    }
    return r[6] == 0xAB && (r[5] & 0x7F) > 0;
}

static esp_err_t touch_init(void)
{
    /* Reset (SensorLib: RST low, then high, 100 ms each; 20 ms low is enough). */
    const gpio_config_t rst = {.pin_bit_mask = 1ULL << PIN_TOUCH_RST, .mode = GPIO_MODE_OUTPUT};
    ESP_RETURN_ON_ERROR(gpio_config(&rst), TAG, "touch rst");
    gpio_set_level(PIN_TOUCH_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_TOUCH_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    ESP_RETURN_ON_FALSE(i2c_bus_probe(I2C_ADDR_CST226), ESP_ERR_NOT_FOUND, TAG, "no touch chip");
    ESP_RETURN_ON_ERROR(i2c_bus_add(I2C_ADDR_CST226, &s_touch), TAG, "touch add");
    const gpio_config_t irq = {
        .pin_bit_mask = 1ULL << PIN_TOUCH_IRQ,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_LOW_LEVEL,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&irq), TAG, "touch irq");
    gpio_intr_disable(PIN_TOUCH_IRQ); /* armed by the button task */
    ESP_RETURN_ON_ERROR(gpio_wakeup_enable(PIN_TOUCH_IRQ, GPIO_INTR_LOW_LEVEL), TAG, "touch wake");
    return ESP_OK;
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

static void button_task(void *arg)
{
    (void)arg;
    key_press_t kp;
    key_press_init(&kp);
    click_counter_t cc;
    click_counter_init(&cc);
    uint32_t last = now_ms();
    bool touch_armed = false;
    uint32_t touch_quiet_until = now_ms();
    for (;;) {
        uint32_t t = now_ms();
        /* Released and settled: arm the level interrupt and sleep until a press, a touch or a
         * click count is due (the chip light-sleeps meanwhile). Otherwise poll the level. */
        TickType_t wait = pdMS_TO_TICKS(BUTTON_POLL_MS);
        const bool down = gpio_get_level(PIN_BUTTON) == 0;
        if (!down && key_press_idle(&kp)) {
            gpio_intr_enable(PIN_BUTTON);
            uint32_t due = click_counter_wait_ms(&cc, t);
            if (s_have_touch && !touch_armed && (int32_t)(touch_quiet_until - t) > 0) {
                /* Wake when the quiet time is over. After it, the touch line below polls
                 * every TOUCH_POLL_MS; a 0 here made that every tick while a finger rested. */
                const uint32_t quiet = touch_quiet_until - t;
                due = quiet < due ? quiet : due;
            }
            wait = due == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(due) + 1;
        }
        /* The touch line: armed again once the quiet time after a tap is over and the finger
         * is off the glass (the chip's report shows no point), so a resting finger is one
         * tap. Until then, read the report every TOUCH_POLL_MS (which also lets INT go). */
        if (s_have_touch && !touch_armed && (int32_t)(t - touch_quiet_until) >= 0) {
            if (!touch_read_tap() && gpio_get_level(PIN_TOUCH_IRQ) == 1) {
                touch_armed = true;
                gpio_intr_enable(PIN_TOUCH_IRQ);
            } else {
                const TickType_t poll = pdMS_TO_TICKS(TOUCH_POLL_MS);
                wait = wait < poll ? wait : poll;
            }
        }
        uint32_t bits = 0;
        xTaskNotifyWait(0, UINT32_MAX, &bits, wait);
        gpio_intr_disable(PIN_BUTTON);

        t = now_ms();
        if (bits & NOTE_TOUCH) {
            touch_armed = false; /* the ISR masked it */
            if (touch_read_tap()) {
                emit(BOARD_BUTTON_BOOT, BOARD_PRESS_SHORT, "tap (status)");
                touch_quiet_until = t + TOUCH_QUIET_MS;
            }
        }
        const uint32_t elapsed = t - last;
        last = t;
        int clicks = 0;
        switch (key_press_update(&kp, gpio_get_level(PIN_BUTTON) == 0, elapsed)) {
        case KEY_PRESS_SHORT: act(click_counter_short(&cc, t, &clicks), clicks); break;
        case KEY_PRESS_LONG: act(click_counter_long(&cc, t), 0); break;
        default: break;
        }
        act(click_counter_poll(&cc, t, &clicks), clicks);
    }
}

static esp_err_t buttons_init(void)
{
    /* gpio_config() also takes the pin back from RTC IO after a standby wake. */
    const gpio_config_t btn = {
        .pin_bit_mask = 1ULL << PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_LOW_LEVEL,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&btn), TAG, "button gpio");
    gpio_intr_disable(PIN_BUTTON); /* armed by the task once the button is up */
    ESP_RETURN_ON_ERROR(gpio_wakeup_enable(PIN_BUTTON, GPIO_INTR_LOW_LEVEL), TAG, "button wake");
    ESP_RETURN_ON_ERROR(esp_sleep_enable_gpio_wakeup(), TAG, "gpio wake");
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(PIN_BUTTON, line_isr,
                                             (void *)(uintptr_t)((PIN_BUTTON << 8) | NOTE_BUTTON)),
                        TAG, "button isr");
    if (touch_init() == ESP_OK) {
        ESP_RETURN_ON_ERROR(gpio_isr_handler_add(PIN_TOUCH_IRQ, line_isr,
                                                 (void *)(uintptr_t)((PIN_TOUCH_IRQ << 8) | NOTE_TOUCH)),
                            TAG, "touch isr");
        s_have_touch = true;
        ESP_LOGI(TAG, "touch: a tap on the screen shows the status");
    } else {
        ESP_LOGW(TAG, "touch chip not found: tap-to-wake off");
    }
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
    /* BOOT is the only button; it stands in for both of the Ultra's. */
    (void)button;
    return gpio_get_level(PIN_BUTTON) == 0;
}

bool board_screen_on_while_recording(void)
{
    return true; /* a desk recorder, mostly on USB: show that it records, all the time */
}

const char *board_button_hint(void)
{
    return "1X REC  2X MUTE  TAP INFO  HOLD NOTE";
}

esp_err_t board_haptic_pulse(int count)
{
    (void)count;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t board_haptic_long(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

/* -------------------------------------------------------------- mic/storage ---- */

board_mic_pins_t board_mic_pins(void)
{
    return (board_mic_pins_t){
        .kind = BOARD_MIC_I2S,
        .clk_gpio = PIN_MIC_BCLK,
        .ws_gpio = PIN_MIC_WS,
        .data_gpio = PIN_MIC_DATA_A,
        .data2_gpio = PIN_MIC_DATA_B,
    };
}

int board_mic_eq(const board_mic_eq_band_t **bands)
{
    /* INMP441s on bare boards: flat. Measure once the array has its case. */
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
    ESP_RETURN_ON_ERROR(board_rail_set(BOARD_RAIL_DISPLAY, true), TAG, "power");
    vTaskDelay(pdMS_TO_TICKS(20)); /* the panel's power IC ramps up */
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
    display_deinit(); /* pins Hi-Z before the power goes off */
    return board_rail_set(BOARD_RAIL_DISPLAY, false);
}

/* ----------------------------------------------------------------- init ---- */

static void scan_i2c(void)
{
    char found[96] = "";
    int n = 0;
    for (int a = 0x08; a < 0x78; a++) {
        if (i2c_bus_probe((uint8_t)a)) {
            char one[8];
            snprintf(one, sizeof(one), " 0x%02X", a);
            strncat(found, one, sizeof(found) - strlen(found) - 1);
            n++;
        }
    }
    ESP_LOGI(TAG, "i2c scan: %d device(s):%s (0x%02X SY6970 charger, 0x%02X CST226 touch)", n,
             n ? found : " none", I2C_ADDR_SY6970, I2C_ADDR_CST226);
}

esp_err_t board_init(void)
{
    s_storage_lock = xSemaphoreCreateMutexStatic(&s_storage_lock_buf);
    ESP_LOGI(TAG, "T4-S3 bring-up");

    /* Display power IC off until the screen is wanted (input enabled: it reads back). */
    const gpio_config_t en = {
        .pin_bit_mask = 1ULL << PIN_DISP_EN,
        .mode = GPIO_MODE_INPUT_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&en), TAG, "disp en");
    gpio_set_level(PIN_DISP_EN, 0);

    /* Unused lines Hi-Z: SD card, panel TE. */
    const int unused[] = {PIN_SD_CS, PIN_SD_MOSI, PIN_SD_SCK, PIN_SD_MISO, PIN_DISP_TE};
    for (unsigned i = 0; i < sizeof(unused) / sizeof(unused[0]); i++) {
        hiz_pin(unused[i]);
    }
    const gpio_config_t irq = {
        .pin_bit_mask = 1ULL << PIN_PMU_IRQ,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&irq);

    ESP_RETURN_ON_ERROR(i2c_bus_init(PIN_I2C_SDA, PIN_I2C_SCL), TAG, "i2c");
    scan_i2c();
    if (sy6970_init(I2C_ADDR_SY6970, BOARD_CHARGE_CURRENT_MA, true) == ESP_OK) {
        s_have_charger = true;
        sy6970_wait_adc(1500); /* the first battery reading */
    } else {
        ESP_LOGE(TAG, "SY6970 charger not found");
    }

    int64_t epoch = 0;
    bool valid = false;
    char buf[24];
    board_rtc_get(&epoch, &valid);
    isotime_format(epoch, buf, sizeof(buf));
    ESP_LOGI(TAG, "clock reads %s (%s)", buf,
             valid ? "set before the last reset" : "not set since power-on");

    ESP_RETURN_ON_ERROR(buttons_init(), TAG, "buttons");

    board_log_power();
    board_log_expander();
    return ESP_OK;
}
