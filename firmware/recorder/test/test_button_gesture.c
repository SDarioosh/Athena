/* Host tests for the button gesture machine (main/button_gesture.c). */
#include <stdbool.h>
#include <stdint.h>

#include "button_gesture.h"
#include "check.h"

#define TICK_MS 10

typedef struct {
    button_gesture_event_t ev;
    uint32_t at_ms;
} logged_t;

static button_gesture_t g_g;
static uint32_t g_now;
static logged_t g_log[16];
static int g_n;

static void reset(void)
{
    button_gesture_init(&g_g);
    g_now = 0;
    g_n = 0;
}

/* Hold the raw level for `ms`, polling every TICK_MS like the board's button task. */
static void level(bool down, uint32_t ms)
{
    for (uint32_t t = 0; t < ms; t += TICK_MS) {
        g_now += TICK_MS;
        const button_gesture_event_t ev = button_gesture_update(&g_g, down, TICK_MS);
        if (ev != BUTTON_GESTURE_NONE && g_n < 16) {
            g_log[g_n].ev = ev;
            g_log[g_n].at_ms = g_now;
            g_n++;
        }
    }
}

static void test_single(void)
{
    reset();
    level(false, 100);
    level(true, 120);
    level(false, 200);
    CHECK_EQ(g_n, 0); /* still waiting for a possible second press */
    level(false, 300);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_SINGLE);
    /* release accepted 30 ms after it (at 260); SINGLE 300 ms after that */
    CHECK(g_log[0].at_ms >= 540 && g_log[0].at_ms <= 570);
    /* A press of half a second is still a single press. */
    reset();
    level(true, 500);
    level(false, 600);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_SINGLE);
}

static void test_double(void)
{
    reset();
    level(true, 100);
    level(false, 150);
    level(true, 100);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_DOUBLE);
    /* fired on the second press (debounced), not on its release */
    CHECK(g_log[0].at_ms >= 280 && g_log[0].at_ms <= 290);
    level(false, 1000);
    CHECK_EQ(g_n, 1); /* no SINGLE afterwards */

    /* Holding the second press does not turn it into a LONG. */
    reset();
    level(true, 80);
    level(false, 100);
    level(true, 2000);
    level(false, 1000);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_DOUBLE);

    /* Two presses further apart than the window are two singles. */
    reset();
    level(true, 80);
    level(false, 500);
    level(true, 80);
    level(false, 500);
    CHECK_EQ(g_n, 2);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_SINGLE);
    CHECK_EQ(g_log[1].ev, BUTTON_GESTURE_SINGLE);
}

static void test_long(void)
{
    reset();
    level(true, 900);
    CHECK_EQ(g_n, 0);
    level(true, 600);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_LONG);
    /* while still held: 30 ms debounce + 1000 ms */
    CHECK(g_log[0].at_ms >= 1030 && g_log[0].at_ms <= 1050);
    level(false, 1000);
    CHECK_EQ(g_n, 1); /* no SINGLE after a LONG */

    /* A press right after a long press starts a new gesture. */
    level(true, 80);
    level(false, 500);
    CHECK_EQ(g_n, 2);
    CHECK_EQ(g_log[1].ev, BUTTON_GESTURE_SINGLE);
}

static void test_debounce(void)
{
    /* Contact bounce shorter than 30 ms is ignored. */
    reset();
    for (int i = 0; i < 10; i++) {
        level(true, 10);
        level(false, 10);
    }
    level(true, 20);
    level(false, 1000);
    CHECK_EQ(g_n, 0);

    /* Bounce on press and release around a real press: still one SINGLE. */
    reset();
    level(true, 10);
    level(false, 10);
    level(true, 10);
    level(false, 10);
    level(true, 150);
    level(false, 10);
    level(true, 10);
    level(false, 600);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_SINGLE);

    /* A bounce in the middle of a long hold does not cut it. */
    reset();
    level(true, 500);
    level(false, 10);
    level(true, 800);
    CHECK_EQ(g_n, 1);
    CHECK_EQ(g_log[0].ev, BUTTON_GESTURE_LONG);
}

static void test_names(void)
{
    CHECK(button_gesture_name(BUTTON_GESTURE_SINGLE)[0] == 's');
    CHECK(button_gesture_name(BUTTON_GESTURE_DOUBLE)[0] == 'd');
    CHECK(button_gesture_name(BUTTON_GESTURE_LONG)[0] == 'l');
    CHECK(button_gesture_name(BUTTON_GESTURE_NONE)[0] == 'n');
}

int main(void)
{
    test_single();
    test_double();
    test_long();
    test_debounce();
    test_names();
    return checks_done("test_button_gesture");
}
