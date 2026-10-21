#include "pda_sleep.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "pda_app.h"
#include "pda_epd.h"
#include "pda_time.h"
#include "sleep_plan.h"
#include "t5_board.h"
#include "ui_pda.h"

static const char *TAG = "sleep";

#define RTC_MAGIC 0x50534c31u /* "PSL1" */
#define POLL_MS 100
#define BOOT_DEBOUNCE_MS 60
/* The backstop fires this long after the plan's own hard cap: only if the wake loop is
 * stuck (a lock, a wedged task). */
#define BACKSTOP_EXTRA_S 15

typedef enum {
    PH_AWAKE,
    PH_ASLEEP,
    PH_SYNC_WAKE,
} phase_t;

typedef struct {
    uint32_t magic;
    uint32_t phase;
    int64_t target_s; /* when the phone is expected; 0 = no sync wakes */
    /* since power-on, for the console */
    uint32_t wakes, resleeps, syncs, feeds, misses, crashes, backstops;
    int64_t last_wake_s;
    int32_t last_awake_ms;
    char since[PDA_SLEEP_LINE_MAX];
    char synced[PDA_SLEEP_LINE_MAX];
} rtc_state_t;

static RTC_NOINIT_ATTR rtc_state_t s_rtc;
static const sleep_cfg_t CFG = SLEEP_CFG_DEFAULT;
static int64_t s_wake_ms;

static void rtc_check(void)
{
    if (s_rtc.magic != RTC_MAGIC) { /* power-on: RTC memory holds garbage */
        memset(&s_rtc, 0, sizeof(s_rtc));
        s_rtc.magic = RTC_MAGIC;
    }
    s_rtc.since[PDA_SLEEP_LINE_MAX - 1] = '\0';
    s_rtc.synced[PDA_SLEEP_LINE_MAX - 1] = '\0';
}

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static int64_t clock_s(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec;
}

void pda_sleep_synced_line(int64_t t, char *out, size_t cap)
{
    if (t <= 0) {
        out[0] = '\0';
        return;
    }
    char hm[12];
    pda_fmt_hm(t, pda_app_tz_min(), hm, sizeof(hm));
    snprintf(out, cap, "synced %s", hm);
}

/* Doesn't return. target_s 0: BOOT only. */
static void sleep_until(int64_t target_s)
{
    s_rtc.target_s = target_s;
    if (s_rtc.phase == PH_SYNC_WAKE) {
        s_rtc.last_awake_ms = (int32_t)now_ms(); /* since the boot, i.e. the wake */
    }
    s_rtc.phase = PH_ASLEEP;
    t5_board_deep_sleep(target_s ? sleep_plan_sleep_us(&CFG, clock_s(), target_s) : 0);
}

void pda_sleep_enter(const char *since, const char *synced, int override_min)
{
    rtc_check();
    snprintf(s_rtc.since, sizeof(s_rtc.since), "%s", since);
    snprintf(s_rtc.synced, sizeof(s_rtc.synced), "%s", synced);
    if (override_min < 0) {
        ESP_LOGI(TAG, "no sync wakes this time");
        sleep_until(0);
    }
    const int64_t now = clock_s();
    const int64_t target = sleep_plan_target(&CFG, now, ble_pda_last_sync(), override_min);
    ESP_LOGI(TAG, "phone expected in %" PRId64 " s", target - now);
    sleep_until(target);
}

void pda_sleep_check_crash(void)
{
    rtc_check();
    const bool in_wake = s_rtc.phase == PH_SYNC_WAKE;
    s_rtc.phase = PH_AWAKE;
    if (!in_wake) {
        return;
    }
    const esp_reset_reason_t r = esp_reset_reason();
    if (r == ESP_RST_PANIC || r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT ||
        r == ESP_RST_BROWNOUT) {
        s_rtc.crashes++;
        ESP_LOGE(TAG, "reset %d during a sync wake: back to sleep (the next sync wake as planned)",
                 (int)r);
        sleep_until(clock_s() + CFG.interval_s);
    }
}

static void on_backstop(void *arg)
{
    (void)arg;
    ESP_LOGE(TAG, "sync wake stuck: sleeping now");
    s_rtc.backstops++;
    s_rtc.misses++;
    sleep_until(clock_s() + CFG.interval_s);
}

/* A new feed came: change the synced line on the panel, nothing else. The panel shows the
 * Asleep screen as ui_pda_draw_asleep() drew it with the stored lines; draw that again,
 * tell epdiy it is what the panel shows, draw the new line and update just its area. */
static void show_synced(int64_t bye_s)
{
    char line[PDA_SLEEP_LINE_MAX];
    pda_sleep_synced_line(bye_s, line, sizeof(line));
    if (!pda_app_time_valid() || !line[0] || strcmp(line, s_rtc.synced) == 0) {
        return;
    }
    const esp_err_t err = pda_epd_init(t5_board_i2c());
    t5_board_side_button_input(); /* epdiy made all of PCA9535 port 1 outputs */
    if (err != ESP_OK) {
        return;
    }
    ui_pda_draw_asleep(s_rtc.since, s_rtc.synced);
    pda_epd_mark_shown();
    ui_pda_draw_asleep(s_rtc.since, line);
    pda_epd_update_light(ui_pda_asleep_synced_area());
    snprintf(s_rtc.synced, sizeof(s_rtc.synced), "%s", line);
    ESP_LOGI(TAG, "Asleep screen: \"%s\"", line);
}

void pda_sleep_sync_wake(const ble_pda_hooks_t *hooks)
{
    s_wake_ms = now_ms();
    rtc_check();
    s_rtc.phase = PH_SYNC_WAKE;
    s_rtc.wakes++;

    /* The PCF8563 keeps better time than the deep-sleep timer: decide on its clock. */
    int64_t now = 0;
    bool valid = false;
    if (t5_rtc_get(&now, &valid) == ESP_OK && valid) {
        const struct timeval tv = {.tv_sec = (time_t)now};
        settimeofday(&tv, NULL);
    }
    now = clock_s();
    s_rtc.last_wake_s = now;
    if (s_rtc.target_s == 0) {
        s_rtc.target_s = now; /* shouldn't happen (no timer was set): just look around */
    }
    uint64_t arg = 0;
    if (sleep_plan_on_wake(&CFG, now, s_rtc.target_s, &arg) == SLEEP_WAKE_AGAIN) {
        ESP_LOGI(TAG, "woke %" PRId64 " s early: sleeping again", s_rtc.target_s - now);
        s_rtc.resleeps++;
        s_rtc.phase = PH_ASLEEP;
        t5_board_deep_sleep(arg);
    }
    const int64_t window_ms = (int64_t)arg;
    ESP_LOGI(TAG, "sync wake %" PRIu32 ": phone due in %" PRId64 " s, awake for %" PRId64 " s",
             s_rtc.wakes, s_rtc.target_s - now, window_ms / 1000);

    esp_timer_handle_t backstop = NULL;
    const esp_timer_create_args_t bt = {.callback = on_backstop, .name = "sleep_backstop"};
    if (esp_timer_create(&bt, &backstop) == ESP_OK) {
        esp_timer_start_once(backstop, (uint64_t)(CFG.max_awake_s + BACKSTOP_EXTRA_S) * 1000000u);
    }

    if (pda_app_init() != ESP_OK) {
        ESP_LOGE(TAG, "storage unavailable: no sync this time");
        s_rtc.misses++;
        sleep_until(clock_s() + CFG.interval_s);
    }
    const uint32_t feed_before = pda_app_feed_version();
    ble_pda_set_fast_adv(true); /* no pairing window: bonded relays only */
    if (ble_pda_init(pda_app_store(), NULL, hooks) != ESP_OK) {
        ESP_LOGE(TAG, "BLE unavailable: no sync this time");
        s_rtc.misses++;
        sleep_until(clock_s() + CFG.interval_s);
    }

    sleep_wake_state_t st = {.window_ms = window_ms, .bye_ms = -1, .link_down_ms = -1};
    bool was_connected = false;
    int64_t boot_down_ms = -1;
    sleep_step_t step;
    for (;;) {
        pda_ev_t ev;
        pda_app_wait_event(&ev, POLL_MS); /* the UI's events: nobody else reads them */
        const int64_t t = now_ms();
        st.elapsed_ms = t - s_wake_ms;
        st.connected = ble_pda_is_connected();
        if (st.connected) {
            was_connected = true;
        } else if (was_connected) {
            was_connected = false;
            if (st.bye_ms < 0) {
                st.link_down_ms = st.elapsed_ms;
                ESP_LOGW(TAG, "link lost before BYE: waiting for the phone's retry");
            }
        }
        if (st.bye_ms < 0 && ble_pda_last_sync() != 0) {
            st.bye_ms = st.elapsed_ms;
        }
        if (t5_button_is_down(T5_BUTTON_BOOT)) {
            if (boot_down_ms < 0) {
                boot_down_ms = t;
            }
            st.boot_pressed = t - boot_down_ms >= BOOT_DEBOUNCE_MS;
        } else {
            boot_down_ms = -1;
        }
        step = sleep_plan_step(&CFG, &st);
        if (step != SLEEP_STEP_WAIT) {
            break;
        }
    }

    if (step == SLEEP_STEP_WAKE_UI) {
        if (backstop) {
            esp_timer_stop(backstop);
        }
        s_rtc.phase = PH_AWAKE;
        t5_board_restart_awake();
    }
    const int64_t bye_s = ble_pda_last_sync();
    if (bye_s) {
        s_rtc.syncs++;
    } else {
        s_rtc.misses++;
    }
    pda_app_lock(); /* no feed or outbox write from the BLE task from here on */
    if (bye_s && pda_app_feed_version() != feed_before) {
        s_rtc.feeds++;
        show_synced(bye_s);
    }
    now = clock_s();
    const int64_t target = sleep_plan_target(&CFG, now, bye_s, 0);
    ESP_LOGI(TAG, "%s after %" PRId64 " s; phone next expected in %" PRId64 " s",
             bye_s ? "synced" : "no sync", (now_ms() - s_wake_ms) / 1000, target - now);
    sleep_until(target);
}

void pda_sleep_print(void)
{
    rtc_check();
    printf("sync wakes: every %d min when the phone is due (window %d s before, %d s after, "
           "at most %d s)\n",
           CFG.interval_s / 60, CFG.early_s, CFG.after_s, CFG.max_awake_s);
    printf("since power-on: %" PRIu32 " wakes (%" PRIu32 " slept again), %" PRIu32
           " synced, %" PRIu32 " new feeds, %" PRIu32 " missed, %" PRIu32 " crashed, %" PRIu32
           " stuck\n",
           s_rtc.wakes, s_rtc.resleeps, s_rtc.syncs, s_rtc.feeds, s_rtc.misses, s_rtc.crashes,
           s_rtc.backstops);
    if (s_rtc.last_wake_s) {
        char hm[12];
        pda_fmt_hm(s_rtc.last_wake_s, pda_app_tz_min(), hm, sizeof(hm));
        printf("last sync wake: %s, awake %d ms\n", hm, (int)s_rtc.last_awake_ms);
    }
}
