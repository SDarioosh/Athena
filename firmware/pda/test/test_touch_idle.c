/* The touch task's idle policy: INT trigger modes and the safety-net poll period. */
#include <string.h>

#include "check.h"
#include "gesture.h"
#include "touch_idle.h"

int main(void)
{
    /* 0x804D bits 1:0; the other bits (e.g. axis swaps) don't matter */
    CHECK_EQ(touch_trig_active_level(0x00), 1);
    CHECK_EQ(touch_trig_active_level(0x01), 0);
    CHECK_EQ(touch_trig_active_level(0x02), 0);
    CHECK_EQ(touch_trig_active_level(0x03), 1);
    CHECK_EQ(touch_trig_active_level(0x3d), 0); /* 0b111101: falling */
    CHECK(strcmp(touch_trig_name(0x0d), "falling edge") == 0);
    CHECK(strcmp(touch_trig_name(0x0e), "low level") == 0);

    touch_idle_t t = {0};
    /* INT stuck active: only polling, fast enough for a tap */
    CHECK_EQ(touch_idle_wait_ms(&t, false), TOUCH_UNARMED_POLL_MS);
    /* not proven yet: the safety net */
    CHECK_EQ(touch_idle_wait_ms(&t, true), TOUCH_IDLE_POLL_MS);
    for (int i = 0; i < TOUCH_INT_TRUST_AFTER - 1; i++) {
        touch_idle_note(&t, true);
    }
    CHECK(!touch_idle_trusted(&t));
    CHECK_EQ(touch_idle_wait_ms(&t, true), TOUCH_IDLE_POLL_MS);
    touch_idle_note(&t, true);
    CHECK(touch_idle_trusted(&t));
    CHECK_EQ(touch_idle_wait_ms(&t, true), TOUCH_IDLE_TRUSTED_MS);
    CHECK_EQ(touch_idle_wait_ms(&t, false), TOUCH_UNARMED_POLL_MS);

    /* one touch INT missed: back to the safety-net rate for good */
    touch_idle_note(&t, false);
    CHECK(!touch_idle_trusted(&t));
    for (int i = 0; i < 10; i++) {
        touch_idle_note(&t, true);
    }
    CHECK_EQ(touch_idle_wait_ms(&t, true), TOUCH_IDLE_POLL_MS);

    CHECK(TOUCH_STALE_MS > GESTURE_LONG_MS); /* never cuts a long press short */
    return checks_done("test_touch_idle");
}
