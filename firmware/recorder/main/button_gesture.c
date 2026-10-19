#include "button_gesture.h"

#include <string.h>

enum {
    PHASE_IDLE,     /* released, nothing pending */
    PHASE_PRESSED,  /* first press held, not yet long */
    PHASE_WAIT2,    /* first press released: a second press within double_ms is a DOUBLE */
    PHASE_IGNORE,   /* held after a LONG or a DOUBLE: wait for the release */
};

void button_gesture_init(button_gesture_t *g)
{
    memset(g, 0, sizeof(*g));
    g->debounce_ms = BUTTON_GESTURE_DEBOUNCE_MS;
    g->long_ms = BUTTON_GESTURE_LONG_MS;
    g->double_ms = BUTTON_GESTURE_DOUBLE_MS;
    g->phase = PHASE_IDLE;
}

button_gesture_event_t button_gesture_update(button_gesture_t *g, bool down, uint32_t elapsed_ms)
{
    /* Debounce: an edge is a raw level that differs from the stable one for debounce_ms. */
    bool pressed = false, released = false;
    if (down != g->candidate) {
        g->candidate = down;
        g->cand_ms = 0;
    } else {
        g->cand_ms += elapsed_ms;
    }
    if (g->candidate != g->stable && g->cand_ms >= g->debounce_ms) {
        g->stable = g->candidate;
        pressed = g->stable;
        released = !g->stable;
    }

    switch (g->phase) {
    case PHASE_IDLE:
        if (pressed) {
            g->phase = PHASE_PRESSED;
            g->phase_ms = 0;
        }
        break;
    case PHASE_PRESSED:
        if (released) {
            g->phase = PHASE_WAIT2;
            g->phase_ms = 0;
            break;
        }
        g->phase_ms += elapsed_ms;
        if (g->phase_ms >= g->long_ms) {
            g->phase = PHASE_IGNORE;
            return BUTTON_GESTURE_LONG;
        }
        break;
    case PHASE_WAIT2:
        if (pressed) {
            g->phase = PHASE_IGNORE;
            return BUTTON_GESTURE_DOUBLE;
        }
        g->phase_ms += elapsed_ms;
        if (g->phase_ms >= g->double_ms) {
            g->phase = PHASE_IDLE;
            return BUTTON_GESTURE_SINGLE;
        }
        break;
    case PHASE_IGNORE:
    default:
        if (released || !g->stable) {
            g->phase = PHASE_IDLE;
        }
        break;
    }
    return BUTTON_GESTURE_NONE;
}

const char *button_gesture_name(button_gesture_event_t ev)
{
    switch (ev) {
    case BUTTON_GESTURE_SINGLE: return "single";
    case BUTTON_GESTURE_DOUBLE: return "double";
    case BUTTON_GESTURE_LONG: return "long";
    case BUTTON_GESTURE_NONE:
    default: return "none";
    }
}
