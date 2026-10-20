#include "gesture.h"

#include <stdlib.h>

void gesture_reset(gesture_state_t *g)
{
    *g = (gesture_state_t){0};
}

gesture_t gesture_feed(gesture_state_t *g, bool touching, int x, int y, int64_t now_ms)
{
    gesture_t out = {.kind = GESTURE_NONE};
    if (touching) {
        if (!g->down) {
            *g = (gesture_state_t){.down = true, .x0 = x, .y0 = y, .x = x, .y = y, .t0 = now_ms};
            return out;
        }
        g->x = x;
        g->y = y;
        g->moved |= abs(x - g->x0) > GESTURE_MOVE_PX || abs(y - g->y0) > GESTURE_MOVE_PX;
        if (!g->reported && !g->moved && now_ms - g->t0 >= GESTURE_LONG_MS) {
            g->reported = true;
            out = (gesture_t){.kind = GESTURE_LONG, .x = g->x0, .y = g->y0};
        }
        return out;
    }
    if (!g->down) {
        return out;
    }
    /* Finger lifted: classify with the last position seen while touching. */
    g->down = false;
    if (g->reported) {
        return out;
    }
    const int dx = g->x - g->x0, dy = g->y - g->y0;
    out.x = g->x0;
    out.y = g->y0;
    if (abs(dx) >= GESTURE_SWIPE_PX && abs(dx) >= 2 * abs(dy)) {
        out.kind = dx < 0 ? GESTURE_SWIPE_LEFT : GESTURE_SWIPE_RIGHT;
    } else if (abs(dy) >= GESTURE_SWIPE_PX && abs(dy) >= 2 * abs(dx)) {
        out.kind = dy < 0 ? GESTURE_SWIPE_UP : GESTURE_SWIPE_DOWN;
    } else if (!g->moved && now_ms - g->t0 <= GESTURE_TAP_MAX_MS) {
        out.kind = GESTURE_TAP;
    }
    return out;
}

const char *gesture_name(gesture_kind_t k)
{
    switch (k) {
    case GESTURE_TAP: return "tap";
    case GESTURE_LONG: return "long";
    case GESTURE_SWIPE_LEFT: return "swipe-left";
    case GESTURE_SWIPE_RIGHT: return "swipe-right";
    case GESTURE_SWIPE_UP: return "swipe-up";
    case GESTURE_SWIPE_DOWN: return "swipe-down";
    default: return "none";
    }
}
