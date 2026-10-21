/* How the touch task waits while no finger is down (touch_gt911.c). Pure C; host-tested
 * (test/test_touch_idle.c).
 *
 * The task sleeps on the GT911 INT line. Nobody has checked yet how INT behaves on the T5
 * (trigger mode, pulse length, whether a pulse survives a wake from light sleep), so there is
 * a safety net: a status poll every TOUCH_IDLE_POLL_MS even when INT stays quiet. Once INT
 * has started TOUCH_INT_TRUST_AFTER touches and no poll has found a touch INT missed, the net
 * slows to TOUCH_IDLE_TRUSTED_MS. One touch found by a poll first puts it back to
 * TOUCH_IDLE_POLL_MS for good (until reset). With the INT line stuck at its active level
 * (level mode or wrong polarity) the task can't arm it and polls every TOUCH_UNARMED_POLL_MS:
 * slower than the 25 ms of a touch in progress, fast enough to catch a quick tap. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TOUCH_IDLE_POLL_MS 250
#define TOUCH_IDLE_TRUSTED_MS 1000
#define TOUCH_UNARMED_POLL_MS 50
#define TOUCH_INT_TRUST_AFTER 3
/* A finger reported down with no new sample for this long counts as lifted: a lost
 * release frame must not keep the task polling forever. Well past a long press (700 ms). */
#define TOUCH_STALE_MS 2000

typedef struct {
    uint32_t by_int;  /* touches whose first sample came after an INT */
    uint32_t by_poll; /* touches a safety-net poll found first */
} touch_idle_t;

/* GT911 config register 0x804D bits 1:0 (trigger mode). */
typedef enum {
    TOUCH_TRIG_RISING = 0,
    TOUCH_TRIG_FALLING = 1,
    TOUCH_TRIG_LOW = 2,
    TOUCH_TRIG_HIGH = 3,
} touch_trig_t;

/* The INT level that means "data ready": 1 for rising/high, 0 for falling/low. */
int touch_trig_active_level(uint8_t reg_804d);
const char *touch_trig_name(uint8_t reg_804d);

/* A touch began; by_int: the task was woken by INT, not by its poll timeout. */
void touch_idle_note(touch_idle_t *t, bool by_int);

/* INT is trusted: it started enough touches and never missed one. */
bool touch_idle_trusted(const touch_idle_t *t);

/* How long to wait for INT before polling anyway. armed: the INT interrupt is enabled
 * (the line was idle); otherwise the task can only poll. */
int touch_idle_wait_ms(const touch_idle_t *t, bool armed);

#ifdef __cplusplus
}
#endif
