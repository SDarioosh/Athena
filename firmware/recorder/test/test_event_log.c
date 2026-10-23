/* Host tests for the event log's pure part (main/event_log.c). */
#include <string.h>

#include "check.h"
#include "event_log.h"

static el_entry_t ev(uint8_t kind, uint8_t why, int32_t arg)
{
    return (el_entry_t){.t = 1792762828, .up_s = 62000, .kind = kind, .why = why, .arg = arg,
                        .batt = 73};
}

static void test_ring_order_and_wrap(void)
{
    static el_ring_t r;
    el_ring_init(&r, 0);
    CHECK(el_ring_get(&r, 0) == NULL);
    for (int i = 0; i < 3; i++) {
        const el_entry_t e = ev(EL_BUTTON, EL_BTN_PWR_SHORT, i);
        CHECK_EQ(el_ring_add(&r, &e), 0);
    }
    /* Oldest first: the empty slots come first, then 0, 1, 2. */
    int seen = 0, last = -1;
    for (unsigned i = 0; i < EVENT_LOG_CAP; i++) {
        const el_entry_t *e = el_ring_get(&r, i);
        if (e) {
            CHECK_EQ(e->arg, last + 1);
            last = e->arg;
            seen++;
        }
    }
    CHECK_EQ(seen, 3);

    /* Past the capacity the oldest go, and order holds. */
    for (int i = 3; i < EVENT_LOG_CAP + 5; i++) {
        const el_entry_t e = ev(EL_BUTTON, EL_BTN_PWR_SHORT, i);
        CHECK_EQ(el_ring_add(&r, &e), (unsigned)(i % EVENT_LOG_CAP) / EVENT_LOG_BLOCK);
    }
    /* Slot 5 started block 0 again: slots 5..7 were cleared, so only 128 - 3 remain. */
    seen = 0;
    last = -1;
    for (unsigned i = 0; i < EVENT_LOG_CAP; i++) {
        const el_entry_t *e = el_ring_get(&r, i);
        if (e) {
            if (last >= 0) {
                CHECK_EQ(e->arg, last + 1);
            }
            last = e->arg;
            seen++;
        }
    }
    CHECK_EQ(seen, EVENT_LOG_CAP - 3);
    CHECK_EQ(last, EVENT_LOG_CAP + 4);
}

static void test_ring_counter_wraps_cleanly(void)
{
    static el_ring_t r;
    el_ring_init(&r, 65535); /* slot 127 */
    const el_entry_t a = ev(EL_MUTE, 1, 0), b = ev(EL_MUTE, 0, 0);
    CHECK_EQ(el_ring_add(&r, &a), EVENT_LOG_BLOCKS - 1);
    CHECK_EQ(r.next, 0);
    CHECK_EQ(el_ring_add(&r, &b), 0); /* 65536 % 128 == 0: slot 0 follows slot 127 */
    const el_entry_t *last = el_ring_get(&r, EVENT_LOG_CAP - 1);
    CHECK(last && last->why == 0);
    const el_entry_t *prev = el_ring_get(&r, EVENT_LOG_CAP - 2);
    CHECK(prev && prev->why == 1);
}

static void test_format(void)
{
    char line[EVENT_LOG_LINE];
    el_entry_t e = ev(EL_BUTTON, EL_BTN_PWR_SHORT, 0x000b10);
    el_format(&e, line, sizeof(line));
    CHECK(strcmp(line, "2026-10-23T13:40:28Z +62000s  button PWR short (PMU 0x000b10: key edges "
                       "+ -); batt 73%") == 0);

    e = ev(EL_BUTTON, EL_BTN_PWR_SHORT, 0x000800); /* the PMU's decision, no key edge */
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "no key edges") != NULL);

    e = ev(EL_BUTTON, EL_BTN_PWR_SHORT_IGNORED, 640);
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "PWR short ignored (640 ms after the previous one)") != NULL);

    e = ev(EL_END, EL_END_ERROR, 23);
    e.aux = EL_REC_CONVERSATION;
    e.batt = -1;
    e.t = 0;
    el_format(&e, line, sizeof(line));
    CHECK(strcmp(line, "(clock unknown) +62000s  conversation ended (error) after 23 s") == 0);

    e = ev(EL_START, EL_TRIG_RESUME, 0);
    e.aux = EL_REC_CONVERSATION;
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "conversation started by resume after a restart") != NULL);

    e = ev(EL_BOOT, EL_RST_TASK_WDT, 2);
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "boot: task watchdog, recording resumed") != NULL);

    e = ev(EL_CARD_RESET, 1, 0x107);
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "write error 0x107: mounted again") != NULL);

    /* Unknown values never crash or overflow. */
    e = ev(200, 250, -1);
    el_format(&e, line, sizeof(line));
    CHECK(strstr(line, "entry kind 200") != NULL);
    CHECK(strcmp(el_trigger_name(99), "?") == 0);
    CHECK(strcmp(el_button_name(0), "?") == 0);
    CHECK(strcmp(el_reset_name(-1), "?") == 0);
}

int main(void)
{
    test_ring_order_and_wrap();
    test_ring_counter_wraps_cleanly();
    test_format();
    return checks_done("test_event_log");
}
