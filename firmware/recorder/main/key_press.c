#include "key_press.h"

void key_press_init(key_press_t *k)
{
    *k = (key_press_t){.debounce_ms = KEY_PRESS_DEBOUNCE_MS, .long_ms = KEY_PRESS_LONG_MS};
}

key_press_event_t key_press_update(key_press_t *k, bool down, uint32_t elapsed_ms)
{
    if (down != k->candidate) {
        k->candidate = down;
        k->cand_ms = 0;
    } else if (k->cand_ms < UINT32_MAX - elapsed_ms) {
        k->cand_ms += elapsed_ms;
    }
    if (k->stable) {
        k->held_ms += elapsed_ms;
    }

    if (k->candidate != k->stable && k->cand_ms >= k->debounce_ms) {
        k->stable = k->candidate;
        if (k->stable) {
            /* The press began when the level first went down. */
            k->held_ms = k->cand_ms;
            k->long_sent = false;
        } else {
            const bool was_long = k->long_sent;
            k->long_sent = false;
            k->held_ms = 0;
            return was_long ? KEY_PRESS_NONE : KEY_PRESS_SHORT;
        }
    }
    if (k->stable && !k->long_sent && k->held_ms >= k->long_ms) {
        k->long_sent = true;
        return KEY_PRESS_LONG;
    }
    return KEY_PRESS_NONE;
}

bool key_press_idle(const key_press_t *k)
{
    return !k->stable && !k->candidate;
}
