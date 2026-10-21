#include "touch_idle.h"

int touch_trig_active_level(uint8_t reg_804d)
{
    const int mode = reg_804d & 3;
    return mode == TOUCH_TRIG_RISING || mode == TOUCH_TRIG_HIGH;
}

const char *touch_trig_name(uint8_t reg_804d)
{
    static const char *const names[] = {"rising edge", "falling edge", "low level", "high level"};
    return names[reg_804d & 3];
}

void touch_idle_note(touch_idle_t *t, bool by_int)
{
    if (by_int) {
        t->by_int++;
    } else {
        t->by_poll++;
    }
}

bool touch_idle_trusted(const touch_idle_t *t)
{
    return t->by_poll == 0 && t->by_int >= TOUCH_INT_TRUST_AFTER;
}

int touch_idle_wait_ms(const touch_idle_t *t, bool armed)
{
    if (!armed) {
        return TOUCH_UNARMED_POLL_MS;
    }
    return touch_idle_trusted(t) ? TOUCH_IDLE_TRUSTED_MS : TOUCH_IDLE_POLL_MS;
}
