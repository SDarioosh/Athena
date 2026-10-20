#include "power_samples.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "isotime.h"

const char PS_CSV_HEADER[] = "time,uptime_s,percent,mv,charging,usb,state\n";

void ps_ring_init(ps_ring_t *r, ps_sample_t *buf, size_t cap)
{
    *r = (ps_ring_t){.buf = buf, .cap = cap};
}

void ps_ring_push(ps_ring_t *r, const ps_sample_t *s)
{
    if (!r->cap) {
        return;
    }
    r->buf[r->next] = *s;
    r->next = (r->next + 1) % r->cap;
    if (r->count < r->cap) {
        r->count++;
    }
    if (r->unwritten < r->count) {
        r->unwritten++;
    }
}

bool ps_ring_get(const ps_ring_t *r, size_t i, ps_sample_t *out)
{
    if (i >= r->count) {
        return false;
    }
    const size_t oldest = (r->next + r->cap - r->count) % r->cap;
    *out = r->buf[(oldest + i) % r->cap];
    return true;
}

bool ps_ring_peek_unwritten(const ps_ring_t *r, ps_sample_t *out)
{
    return r->unwritten && ps_ring_get(r, r->count - r->unwritten, out);
}

void ps_ring_mark_written(ps_ring_t *r)
{
    if (r->unwritten) {
        r->unwritten--;
    }
}

void ps_state_name(uint8_t flags, char *out, size_t len)
{
    static const struct {
        uint8_t flag;
        const char *name;
    } NAMES[] = {{PS_RECORDING, "rec"}, {PS_NOTE, "note"},     {PS_BLE_ADV, "ble_adv"},
                 {PS_BLE_CONN, "ble_conn"}, {PS_SCREEN, "screen"}, {PS_SD, "sd"}};
    size_t n = 0;
    out[0] = '\0';
    for (size_t i = 0; i < sizeof(NAMES) / sizeof(NAMES[0]); i++) {
        if (flags & NAMES[i].flag) {
            const int w = snprintf(out + n, len - n, "%s%s", n ? "+" : "", NAMES[i].name);
            if (w < 0 || (size_t)w >= len - n) {
                break;
            }
            n += (size_t)w;
        }
    }
    if (!n) {
        snprintf(out, len, "idle");
    }
}

size_t ps_csv_line(const ps_sample_t *s, char *out, size_t len)
{
    char when[24] = "";
    if (s->epoch > 0) {
        isotime_format(s->epoch, when, sizeof(when));
    }
    char state[48];
    ps_state_name(s->flags, state, sizeof(state));
    const int n = snprintf(out, len, "%s,%" PRIu32 ",%d,%u,%d,%d,%s\n", when, s->uptime_s,
                           s->percent, s->mv, (s->flags & PS_CHARGING) ? 1 : 0,
                           (s->flags & PS_USB) ? 1 : 0, state);
    return n > 0 && (size_t)n < len ? (size_t)n : 0;
}

bool ps_drain_rate(const ps_ring_t *r, uint32_t min_span_s, double *pct_per_h, uint32_t *span_s)
{
    if (!r->count) {
        return false;
    }
    ps_sample_t last, first, s;
    ps_ring_get(r, r->count - 1, &last);
    if ((last.flags & PS_USB) || last.percent < 0) {
        return false;
    }
    first = last;
    for (size_t i = r->count - 1; i-- > 0;) {
        ps_ring_get(r, i, &s);
        if ((s.flags & PS_USB) || s.percent < 0 || s.uptime_s > first.uptime_s) {
            break; /* USB power, an unknown reading, or a reboot ends the run */
        }
        first = s;
    }
    const uint32_t span = last.uptime_s - first.uptime_s;
    if (span_s) {
        *span_s = span;
    }
    if (span < min_span_s || span == 0) {
        return false;
    }
    *pct_per_h = (double)(first.percent - last.percent) * 3600.0 / (double)span;
    return true;
}
