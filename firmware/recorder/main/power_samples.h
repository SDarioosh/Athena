/* Power log samples (pure, host-tested): a ring of battery readings with the watch's state,
 * their CSV form for /sdcard/power.csv, and the drain rate they show (milestone 2, plan
 * §7.3: the AXP2101 has no battery-current reading, so drain comes from % over time). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* What the watch was doing at the sample. */
#define PS_CHARGING 0x01
#define PS_USB 0x02
#define PS_RECORDING 0x04 /* a conversation session */
#define PS_NOTE 0x08      /* a voice note */
#define PS_BLE_ADV 0x10
#define PS_BLE_CONN 0x20
#define PS_SCREEN 0x40
#define PS_SD 0x80        /* card powered */

typedef struct {
    int64_t epoch;     /* UTC seconds, 0 if the clock isn't set */
    uint32_t uptime_s;
    int16_t percent;   /* -1 unknown */
    uint16_t mv;
    uint8_t flags;     /* PS_* */
} ps_sample_t;

typedef struct {
    ps_sample_t *buf;
    size_t cap;
    size_t next;      /* where the next sample goes */
    size_t count;     /* samples held (<= cap) */
    size_t unwritten; /* newest samples not yet on the card (<= count) */
} ps_ring_t;

void ps_ring_init(ps_ring_t *r, ps_sample_t *buf, size_t cap);
/* Add a sample; when full, the oldest goes (written to the card or not). */
void ps_ring_push(ps_ring_t *r, const ps_sample_t *s);
/* The i-th held sample, 0 = oldest. False if i >= count. */
bool ps_ring_get(const ps_ring_t *r, size_t i, ps_sample_t *out);
/* The oldest sample not yet on the card, and marking it written. */
bool ps_ring_peek_unwritten(const ps_ring_t *r, ps_sample_t *out);
void ps_ring_mark_written(ps_ring_t *r);

/* "time,uptime_s,percent,mv,charging,usb,state" */
extern const char PS_CSV_HEADER[];
/* One CSV line with '\n'; state is e.g. "rec+ble_conn+sd" or "idle". Returns its length, or
 * 0 if it didn't fit. */
size_t ps_csv_line(const ps_sample_t *s, char *out, size_t len);
/* "rec+screen" etc. into out; "idle" when no state flag is set. */
void ps_state_name(uint8_t flags, char *out, size_t len);

/* Drain over the newest run of samples without USB power: percent lost per hour, from the
 * first to the last sample of that run. False if the run spans less than min_span_s. */
bool ps_drain_rate(const ps_ring_t *r, uint32_t min_span_s, double *pct_per_h, uint32_t *span_s);

#ifdef __cplusplus
}
#endif
