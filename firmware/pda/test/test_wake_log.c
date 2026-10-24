/* The sleep/wake log's text form and its "never went back to sleep" check (wake_log.c). */
#include <string.h>

#include "check.h"
#include "wake_log.h"

#define T0 1792761445u /* 2026-10-23T13:17:25Z, Fri 09:17:25 at UTC-4 */
#define TZ (-240)

static wl_entry_t boot(int why, int32_t wake)
{
    const wl_entry_t e = {.t = T0, .up_s = 1, .kind = WL_BOOT, .why = (uint8_t)why, .arg = wake,
                          .batt = 97};
    return e;
}

static wl_entry_t sleep_e(int why, int32_t timer_s)
{
    const wl_entry_t e = {.t = T0 + 60, .up_s = 48, .kind = WL_SLEEP, .why = (uint8_t)why,
                          .arg = timer_s, .batt = -1};
    return e;
}

static void test_format(void)
{
    char line[WAKE_LOG_LINE];
    wl_entry_t e = boot(WL_RST_POWERON, WL_WAKE_RESET);
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strcmp(line, "Fri 23 Oct 09:17:25 +1s  boot: power-on or RST, not from sleep; batt 97%") ==
          0);

    e = sleep_e(WL_SLEEP_SYNCED_FEED, 845);
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strcmp(line, "Fri 23 Oct 09:18:25 +48s  sleep: synced, new feed; timer 14 min 5 s") == 0);

    e = sleep_e(WL_SLEEP_USER, 0);
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strstr(line, "sleep: asleep from the UI; BOOT only") != NULL);

    e.t = 0;
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strncmp(line, "(clock unknown) +48s", 20) == 0);

    /* Out-of-range codes from a newer or corrupt entry still print. */
    e = boot(200, 9);
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strstr(line, "boot: ?, ?") != NULL);
    e.kind = 77;
    wl_format(&e, TZ, line, sizeof(line));
    CHECK(strstr(line, "entry kind 77") != NULL);

    /* A tiny buffer is cut, never overrun. */
    char small[8];
    memset(small, 'x', sizeof(small));
    wl_format(&e, TZ, small, sizeof(small));
    CHECK(small[sizeof(small) - 1] == '\0');
}

static void test_missed_sleep(void)
{
    const wl_entry_t timer = boot(WL_RST_DEEPSLEEP, WL_WAKE_TIMER);
    const wl_entry_t ui = boot(WL_RST_DEEPSLEEP, WL_WAKE_BUTTON);
    const wl_entry_t power = boot(WL_RST_POWERON, WL_WAKE_RESET);
    const wl_entry_t slept = sleep_e(WL_SLEEP_NO_SYNC, 900);
    const wl_entry_t restart = {.kind = WL_UI_RESTART};

    CHECK(wl_missed_sleep(&timer, &power)); /* this morning: a sync wake, then a power-on */
    CHECK(wl_missed_sleep(&timer, &timer));
    CHECK(!wl_missed_sleep(&slept, &power));   /* asleep when the power went */
    CHECK(!wl_missed_sleep(&ui, &power));      /* in use: a RST press is normal */
    CHECK(!wl_missed_sleep(&restart, &ui));    /* BOOT during a sync wake */
    CHECK(!wl_missed_sleep(&timer, &slept));
    CHECK(!wl_missed_sleep(NULL, &power));
}

int main(void)
{
    test_format();
    test_missed_sleep();
    return checks_done("test_wake_log");
}
