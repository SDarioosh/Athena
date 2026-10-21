/* Host tests for the GPIO press detector (main/key_press.c). */
#include <stdint.h>

#include "check.h"
#include "key_press.h"

static key_press_t g_k;

/* Hold the level for `ms` in 10 ms steps; count the events seen. */
static void feed(bool down, uint32_t ms, int *shorts, int *longs)
{
    for (uint32_t t = 0; t < ms; t += 10) {
        const key_press_event_t ev = key_press_update(&g_k, down, 10);
        if (ev == KEY_PRESS_SHORT) {
            (*shorts)++;
        } else if (ev == KEY_PRESS_LONG) {
            (*longs)++;
        }
    }
}

static void test_short(void)
{
    key_press_init(&g_k);
    int s = 0, l = 0;
    feed(false, 100, &s, &l);
    CHECK(key_press_idle(&g_k));
    feed(true, 200, &s, &l);
    CHECK_EQ(s, 0);
    CHECK(!key_press_idle(&g_k));
    feed(false, 100, &s, &l);
    CHECK_EQ(s, 1);
    CHECK_EQ(l, 0);
    CHECK(key_press_idle(&g_k));
}

static void test_long_fires_while_held_once(void)
{
    key_press_init(&g_k);
    int s = 0, l = 0;
    feed(true, KEY_PRESS_LONG_MS - 50, &s, &l);
    CHECK_EQ(l, 0);
    feed(true, 100, &s, &l);
    CHECK_EQ(l, 1);
    feed(true, 3000, &s, &l); /* still held: no repeat */
    CHECK_EQ(l, 1);
    feed(false, 100, &s, &l); /* the release after a long is silent */
    CHECK_EQ(s, 0);
    CHECK_EQ(l, 1);
}

static void test_bounce_is_ignored(void)
{
    key_press_init(&g_k);
    int s = 0, l = 0;
    /* 10 ms glitches never last the debounce time. */
    for (int i = 0; i < 20; i++) {
        feed(true, 10, &s, &l);
        feed(false, 10, &s, &l);
    }
    CHECK_EQ(s, 0);
    /* A real press with contact bounce at both ends is one click. */
    feed(true, 10, &s, &l);
    feed(false, 10, &s, &l);
    feed(true, 150, &s, &l);
    feed(false, 10, &s, &l);
    feed(true, 10, &s, &l);
    feed(false, 100, &s, &l);
    CHECK_EQ(s, 1);
    CHECK_EQ(l, 0);
}

static void test_two_clicks(void)
{
    key_press_init(&g_k);
    int s = 0, l = 0;
    feed(true, 120, &s, &l);
    feed(false, 150, &s, &l);
    feed(true, 120, &s, &l);
    feed(false, 150, &s, &l);
    CHECK_EQ(s, 2);
    CHECK_EQ(l, 0);
}

int main(void)
{
    test_short();
    test_long_fires_while_held_once();
    test_bounce_is_ignored();
    test_two_clicks();
    return checks_done("test_key_press");
}
