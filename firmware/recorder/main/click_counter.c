#include "click_counter.h"

#include <string.h>

void click_counter_init(click_counter_t *c)
{
    memset(c, 0, sizeof(*c));
    c->gap_ms = CLICK_COUNTER_GAP_MS;
    c->guard_ms = CLICK_COUNTER_GUARD_MS;
}

static bool guarded(const click_counter_t *c, uint32_t now_ms)
{
    return c->guard_on && (uint32_t)(now_ms - c->guard_from_ms) < c->guard_ms;
}

click_action_t click_counter_poll(click_counter_t *c, uint32_t now_ms, int *clicks)
{
    if (c->pending == 0 || (uint32_t)(now_ms - c->last_ms) < c->gap_ms) {
        return CLICK_ACTION_NONE;
    }
    if (clicks) {
        *clicks = c->pending;
    }
    c->pending = 0;
    /* The action took place when the gap ran out, even if we are told late. */
    c->guard_on = true;
    c->guard_from_ms = c->last_ms + c->gap_ms;
    return CLICK_ACTION_CLICKS;
}

click_action_t click_counter_short(click_counter_t *c, uint32_t now_ms, int *clicks)
{
    const click_action_t due = click_counter_poll(c, now_ms, clicks);
    if (guarded(c, now_ms)) {
        c->guard_from_ms = now_ms;
        return due != CLICK_ACTION_NONE ? due : CLICK_ACTION_IGNORED;
    }
    c->guard_on = false;
    c->pending++;
    c->last_ms = now_ms;
    return due;
}

click_action_t click_counter_long(click_counter_t *c, uint32_t now_ms)
{
    c->pending = 0;
    c->guard_on = true;
    c->guard_from_ms = now_ms;
    return CLICK_ACTION_HOLD;
}

uint32_t click_counter_wait_ms(const click_counter_t *c, uint32_t now_ms)
{
    if (c->pending == 0) {
        return UINT32_MAX;
    }
    const uint32_t since = now_ms - c->last_ms;
    return since >= c->gap_ms ? 0 : c->gap_ms - since;
}
