#include "handsfree.h"

#include <string.h>

void hf_reset(hf_state_t *s)
{
    memset(s, 0, sizeof(*s));
}

static int cancel(hf_state_t *s, hf_cmd_t *out)
{
    if (!s->holding) {
        return 0;
    }
    s->holding = false;
    out->kind = HF_HOLD_CANCEL;
    out->x = s->x;
    out->y = s->y;
    out->shape = s->shape;
    return 1;
}

static int start(hf_state_t *s, bool focused_todo, int64_t now_ms, hf_cmd_t *out)
{
    if (s->holding || !focused_todo) {
        return 0;
    }
    s->holding = true;
    s->hold_start_ms = now_ms;
    if (s->thumb_ms < now_ms) {
        s->thumb_ms = now_ms; /* a thumbs_up gesture is a sighting too */
    }
    *out = (hf_cmd_t){.kind = HF_HOLD_START, .x = s->x, .y = s->y, .shape = s->shape};
    return 1;
}

int hf_on_event(hf_state_t *s, const vision_event_t *ev, bool focused_todo, int64_t now_ms,
                hf_cmd_t out[HF_CMDS_MAX])
{
    int n = 0;
    if (ev->type == VIS_HAND) {
        if (ev->shape == VIS_SHAPE_NONE) {
            n += cancel(s, &out[n]);
            if (s->cursor) {
                s->cursor = false;
                s->shape = VIS_SHAPE_NONE;
                out[n++] = (hf_cmd_t){.kind = HF_CURSOR_HIDE};
            }
            return n;
        }
        s->cursor = true;
        s->x = ev->x;
        s->y = ev->y;
        const vision_shape_t was = s->shape;
        s->shape = ev->shape;
        out[n++] = (hf_cmd_t){.kind = HF_CURSOR, .x = ev->x, .y = ev->y, .shape = ev->shape};
        if (ev->shape == VIS_SHAPE_THUMBS_UP) {
            s->thumb_ms = now_ms;
            if (was != VIS_SHAPE_THUMBS_UP) {
                n += start(s, focused_todo, now_ms, &out[n]);
            }
        } else {
            n += cancel(s, &out[n]);
        }
        return n;
    }
    if (ev->type != VIS_GESTURE || ev->conf < HF_MIN_CONF) {
        return 0;
    }
    if (ev->g == VIS_G_THUMBS_UP) {
        return start(s, focused_todo, now_ms, &out[0]);
    }
    n += cancel(s, &out[n]); /* any other gesture aborts a hold */
    hf_cmd_t c = {.x = s->x, .y = s->y, .shape = s->shape};
    switch (ev->g) {
    case VIS_G_SWIPE_LEFT: c.kind = HF_NEXT; break;
    case VIS_G_SWIPE_RIGHT: c.kind = HF_PREV; break;
    case VIS_G_SWIPE_UP: c.kind = HF_PAGE_DOWN; break;
    case VIS_G_SWIPE_DOWN: c.kind = HF_PAGE_UP; break;
    case VIS_G_POINT: c.kind = s->cursor ? HF_FOCUS_AT : HF_NONE; break;
    case VIS_G_PALM: c.kind = HF_HOME; break;
    case VIS_G_FIST: c.kind = HF_SCREEN_OFF; break;
    default: c.kind = HF_NONE; break;
    }
    if (c.kind != HF_NONE) {
        out[n++] = c;
    }
    return n;
}

int hf_tick(hf_state_t *s, int64_t now_ms, hf_cmd_t *out)
{
    if (!s->holding) {
        return 0;
    }
    if (now_ms - s->thumb_ms > HF_THUMB_FRESH_MS) {
        return cancel(s, out); /* no thumbs-up hand lately: never commit on the timer alone */
    }
    if (now_ms - s->hold_start_ms < HF_HOLD_MS) {
        return 0;
    }
    s->holding = false;
    *out = (hf_cmd_t){.kind = HF_HOLD_COMMIT, .x = s->x, .y = s->y, .shape = s->shape};
    return 1;
}

int hf_focus_changed(hf_state_t *s, hf_cmd_t *out)
{
    return cancel(s, out);
}

int hf_input_lost(hf_state_t *s, hf_cmd_t out[HF_CMDS_MAX])
{
    int n = cancel(s, &out[0]);
    if (s->cursor) {
        s->cursor = false;
        s->shape = VIS_SHAPE_NONE;
        out[n++] = (hf_cmd_t){.kind = HF_CURSOR_HIDE};
    }
    return n;
}

double hf_hold_progress(const hf_state_t *s, int64_t now_ms)
{
    if (!s->holding) {
        return 0;
    }
    const double p = (double)(now_ms - s->hold_start_ms) / HF_HOLD_MS;
    return p < 0 ? 0 : p > 1 ? 1 : p;
}
