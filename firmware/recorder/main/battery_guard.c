#include "battery_guard.h"

void battery_guard_init(battery_guard_t *g, const bg_limits_t *lim)
{
    *g = (battery_guard_t){.lim = *lim};
}

static bool reading_low(const bg_limits_t *lim, const bg_reading_t *r)
{
    return (r->percent >= 0 && r->percent <= lim->low_percent) ||
           (r->millivolts > 0 && r->millivolts < lim->low_mv);
}

static bool reading_ok(const bg_limits_t *lim, const bg_reading_t *r)
{
    if (r->percent < 0) {
        return r->millivolts >= lim->low_mv + BG_MV_HYSTERESIS;
    }
    return r->percent >= lim->resume_percent && r->millivolts >= lim->low_mv;
}

bg_event_t battery_guard_update(battery_guard_t *g, const bg_reading_t *r)
{
    if (!r->present) {
        g->below = 0; /* nothing to judge (no battery, or a failed read) */
        return BG_NONE;
    }
    if (g->low) {
        if (r->vbus || reading_ok(&g->lim, r)) {
            g->low = false;
            g->below = 0;
            return BG_LEAVE_LOW;
        }
        return BG_NONE;
    }
    if (r->vbus || !reading_low(&g->lim, r)) {
        g->below = 0;
        return BG_NONE;
    }
    if (++g->below < BG_LOW_READINGS) {
        return BG_NONE;
    }
    g->low = true;
    return BG_ENTER_LOW;
}
