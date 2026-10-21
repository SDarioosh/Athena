/* Host tests for the click counter (main/click_counter.c). */
#include <stdint.h>

#include "check.h"
#include "click_counter.h"

static click_counter_t g_c;

/* Poll at the time the counter asks for, like the board's button task. */
static click_action_t settle(uint32_t *now, int *clicks)
{
    const uint32_t wait = click_counter_wait_ms(&g_c, *now);
    if (wait == UINT32_MAX) {
        return CLICK_ACTION_NONE;
    }
    *now += wait;
    return click_counter_poll(&g_c, *now, clicks);
}

static void test_single(void)
{
    click_counter_init(&g_c);
    int n = 0;
    uint32_t now = 1000;
    CHECK_EQ(click_counter_short(&g_c, now, &n), CLICK_ACTION_NONE);
    CHECK_EQ(click_counter_wait_ms(&g_c, now), CLICK_COUNTER_GAP_MS);
    CHECK_EQ(click_counter_poll(&g_c, now + CLICK_COUNTER_GAP_MS - 1, &n), CLICK_ACTION_NONE);
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 1);
    CHECK_EQ(now, 1000 + CLICK_COUNTER_GAP_MS);
    CHECK_EQ(click_counter_wait_ms(&g_c, now), UINT32_MAX);
}

static void test_multi(void)
{
    for (int want = 2; want <= 5; want++) {
        click_counter_init(&g_c);
        int n = 0;
        uint32_t now = 5000;
        for (int i = 0; i < want; i++) {
            CHECK_EQ(click_counter_short(&g_c, now, &n), CLICK_ACTION_NONE);
            now += 300; /* a brisk double-click: ~300 ms release to release */
        }
        now -= 300;
        CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
        CHECK_EQ(n, want);
    }
}

static void test_gap_splits(void)
{
    /* Two clicks further apart than the gap, and past the guard: two singles. */
    click_counter_init(&g_c);
    int n = 0;
    uint32_t now = 0;
    click_counter_short(&g_c, now, &n);
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 1);
    now += CLICK_COUNTER_GUARD_MS;
    CHECK_EQ(click_counter_short(&g_c, now, &n), CLICK_ACTION_NONE);
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 1);
}

static void test_guard(void)
{
    click_counter_init(&g_c);
    int n = 0;
    uint32_t now = 0;
    click_counter_short(&g_c, now, &n);
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    /* Ghost presses 0.6 s and 1.4 s after the action: each restarts the guard. */
    CHECK_EQ(click_counter_short(&g_c, now + 600, &n), CLICK_ACTION_IGNORED);
    CHECK_EQ(click_counter_short(&g_c, now + 1400, &n), CLICK_ACTION_IGNORED);
    CHECK_EQ(click_counter_wait_ms(&g_c, now + 1400), UINT32_MAX);
    /* A full guard time after the last ghost, a click counts again. */
    now += 1400 + CLICK_COUNTER_GUARD_MS;
    CHECK_EQ(click_counter_short(&g_c, now, &n), CLICK_ACTION_NONE);
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 1);
}

static void test_late_poll(void)
{
    /* The task was late: the next click arrives after the gap without a poll in between.
     * The old count comes out, and the new click falls in its guard. */
    click_counter_init(&g_c);
    int n = 0;
    click_counter_short(&g_c, 0, &n);
    click_counter_short(&g_c, 200, &n);
    CHECK_EQ(click_counter_short(&g_c, 200 + CLICK_COUNTER_GAP_MS + 100, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 2);
    CHECK_EQ(click_counter_wait_ms(&g_c, 900), UINT32_MAX);
}

static void test_hold(void)
{
    click_counter_init(&g_c);
    int n = 0;
    /* A click, then a hold: only the hold. */
    click_counter_short(&g_c, 0, &n);
    CHECK_EQ(click_counter_long(&g_c, 300), CLICK_ACTION_HOLD);
    CHECK_EQ(click_counter_wait_ms(&g_c, 300), UINT32_MAX);
    /* A hold right after an action is still a hold (never guarded). */
    CHECK_EQ(click_counter_long(&g_c, 500), CLICK_ACTION_HOLD);
    /* A click just after a hold is guarded. */
    CHECK_EQ(click_counter_short(&g_c, 900, &n), CLICK_ACTION_IGNORED);
}

static void test_wraparound(void)
{
    click_counter_init(&g_c);
    int n = 0;
    uint32_t now = UINT32_MAX - 100;
    click_counter_short(&g_c, now, &n);
    click_counter_short(&g_c, now + 250, &n); /* wraps past zero */
    now += 250;
    CHECK_EQ(settle(&now, &n), CLICK_ACTION_CLICKS);
    CHECK_EQ(n, 2);
}

int main(void)
{
    test_single();
    test_multi();
    test_gap_splits();
    test_guard();
    test_late_poll();
    test_hold();
    test_wraparound();
    return checks_done("test_click_counter");
}
