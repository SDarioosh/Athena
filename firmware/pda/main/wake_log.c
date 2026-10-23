#include "wake_log.h"

#include <stdio.h>

#include "pda_time.h"

const char *wl_reset_name(int why)
{
    static const char *const names[WL_RST_COUNT_] = {
        [WL_RST_UNKNOWN] = "unknown reset",
        [WL_RST_POWERON] = "power-on or RST",
        [WL_RST_EXT] = "external reset",
        [WL_RST_SW] = "software restart",
        [WL_RST_PANIC] = "crash (panic)",
        [WL_RST_INT_WDT] = "interrupt watchdog",
        [WL_RST_TASK_WDT] = "task watchdog",
        [WL_RST_WDT] = "watchdog",
        [WL_RST_DEEPSLEEP] = "deep sleep",
        [WL_RST_BROWNOUT] = "brownout",
        [WL_RST_USB] = "USB reset",
        [WL_RST_JTAG] = "JTAG reset",
        [WL_RST_OTHER] = "other reset",
    };
    return why >= 0 && why < WL_RST_COUNT_ && names[why] ? names[why] : "?";
}

const char *wl_sleep_name(int why)
{
    static const char *const names[WL_SLEEP_COUNT_] = {
        [WL_SLEEP_USER] = "asleep from the UI",
        [WL_SLEEP_SYNCED] = "synced",
        [WL_SLEEP_SYNCED_FEED] = "synced, new feed",
        [WL_SLEEP_NO_SYNC] = "no sync (phone didn't come)",
        [WL_SLEEP_EARLY] = "woke early, sleeping again",
        [WL_SLEEP_AFTER_CRASH] = "after a crash in a sync wake",
        [WL_SLEEP_STUCK] = "sync wake stuck (backstop)",
        [WL_SLEEP_NO_STORAGE] = "storage unavailable",
        [WL_SLEEP_NO_BLE] = "BLE unavailable",
    };
    return why > 0 && why < WL_SLEEP_COUNT_ && names[why] ? names[why] : "?";
}

static const char *wake_name(int32_t w)
{
    switch (w) {
    case WL_WAKE_RESET:
        return "not from sleep";
    case WL_WAKE_BUTTON:
        return "BOOT wake";
    case WL_WAKE_TIMER:
        return "sync timer";
    default:
        return "?";
    }
}

void wl_format(const wl_entry_t *e, int tz_min, char *out, size_t cap)
{
    char when[32];
    if (e->t) {
        char date[16], hm[12];
        pda_fmt_date(e->t, tz_min, date, sizeof(date));
        pda_fmt_hm(e->t, tz_min, hm, sizeof(hm));
        snprintf(when, sizeof(when), "%s %s:%02d", date, hm, (int)(e->t % 60));
    } else {
        snprintf(when, sizeof(when), "(clock unknown)");
    }
    char what[80];
    switch (e->kind) {
    case WL_BOOT:
        snprintf(what, sizeof(what), "boot: %s, %s", wl_reset_name(e->why), wake_name(e->arg));
        break;
    case WL_SLEEP:
        if (e->arg > 0) {
            snprintf(what, sizeof(what), "sleep: %s; timer %d min %d s", wl_sleep_name(e->why),
                     (int)(e->arg / 60), (int)(e->arg % 60));
        } else {
            snprintf(what, sizeof(what), "sleep: %s; BOOT only", wl_sleep_name(e->why));
        }
        break;
    case WL_UI_RESTART:
        snprintf(what, sizeof(what), "BOOT during a sync wake: restart into the UI");
        break;
    default:
        snprintf(what, sizeof(what), "entry kind %d", (int)e->kind);
        break;
    }
    char batt[16] = "";
    if (e->batt >= 0) {
        snprintf(batt, sizeof(batt), "; batt %d%%", (int)e->batt);
    }
    snprintf(out, cap, "%s +%us  %s%s", when, (unsigned)e->up_s, what, batt);
}

bool wl_missed_sleep(const wl_entry_t *prev, const wl_entry_t *cur)
{
    return prev && cur && prev->kind == WL_BOOT && cur->kind == WL_BOOT &&
           prev->arg == WL_WAKE_TIMER;
}
