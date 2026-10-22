#include "today_plan.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "pda_time.h"

/* The insight's sections map one to one onto the plan's. */
_Static_assert(TODAY_DO_FIRST == (int)FEED_INSIGHT_FOCUS && TODAY_COMING_UP == (int)FEED_INSIGHT_COMING_UP &&
                   TODAY_ALSO == (int)FEED_INSIGHT_ALSO && TODAY_SECTIONS == (int)FEED_INSIGHT_SECTIONS,
               "plan and insight sections differ");
_Static_assert(TODAY_PLAN_CAP <= TODAY_PLAN_MAX && FEED_MAX_INSIGHT_ITEMS <= TODAY_PLAN_MAX, "plan room");

/* A section being filled: the smallest `cap` keys so far, in key order (equal keys keep
 * the order they came in), and how many were offered in all. */
typedef struct {
    today_item_t items[TODAY_PLAN_MAX];
    int64_t keys[TODAY_PLAN_MAX];
    int n, total, cap;
    int events, events_kept; /* offered / kept */
} pick_t;

static void offer(pick_t *p, int64_t key, const today_item_t *it)
{
    p->total++;
    p->events += it->ref.kind == FEED_REF_EVENT;
    int at = p->n;
    while (at > 0 && p->keys[at - 1] > key) {
        at--;
    }
    if (at >= p->cap) {
        return;
    }
    if (p->n == p->cap) {
        p->events_kept -= p->items[p->cap - 1].ref.kind == FEED_REF_EVENT; /* pushed out */
    }
    p->events_kept += it->ref.kind == FEED_REF_EVENT;
    const int last = p->n < p->cap ? p->n : p->cap - 1;
    for (int k = last; k > at; k--) {
        p->items[k] = p->items[k - 1];
        p->keys[k] = p->keys[k - 1];
    }
    p->items[at] = *it;
    p->keys[at] = key;
    if (p->n < p->cap) {
        p->n++;
    }
}

static void take(today_section_t *s, const pick_t *p)
{
    memcpy(s->items, p->items, sizeof(p->items[0]) * (size_t)p->n);
    s->n = p->n;
    s->more = p->total - p->n;
    s->more_events = p->events - p->events_kept;
}

bool today_is_shopping_list(const char *title)
{
    static const char *const NAMES[] = {"shopping", "groceries", "grocery"};
    if (!title) {
        return false;
    }
    while (*title == ' ') {
        title++;
    }
    size_t len = strlen(title);
    while (len > 0 && title[len - 1] == ' ') {
        len--;
    }
    if (len > 5 && strncasecmp(title + len - 5, " list", 5) == 0) {
        len -= 5;
        while (len > 0 && title[len - 1] == ' ') {
            len--;
        }
    }
    for (size_t i = 0; i < sizeof(NAMES) / sizeof(NAMES[0]); i++) {
        if (strlen(NAMES[i]) == len && strncasecmp(title, NAMES[i], len) == 0) {
            return true;
        }
    }
    return false;
}

int32_t today_event_day(const feed_event_t *e, int tz_min)
{
    return e->all_day ? e->start_day : pda_local_day(e->start, tz_min);
}

bool today_event_over(const feed_event_t *e, int32_t today, int64_t now)
{
    if (e->all_day) {
        return e->start_day < today;
    }
    return (e->end > e->start ? e->end : e->start + 1) <= now;
}

void today_event_when(const feed_event_t *e, int32_t today, int tz_min, char *out, size_t cap)
{
    const int32_t day = today_event_day(e, tz_min);
    char d[24], hm[12];
    if (day == today) {
        snprintf(d, sizeof(d), "today");
    } else {
        pda_fmt_day(day, d, sizeof(d));
    }
    if (e->all_day) {
        snprintf(out, cap, "%s%s", d, day == today ? ", all day" : "");
        return;
    }
    pda_fmt_hm(e->start, tz_min, hm, sizeof(hm));
    snprintf(out, cap, "%s %s", d, hm);
}

/* Sort keys shared by the sections: on one day, items due that day come first, then
 * all-day events, then timed events by local time. All are >= 0 and < 2^40 (days since
 * 1970 up to the year 36000). */
static int64_t todo_key(int32_t day)
{
    return (int64_t)day * 86400 - 2;
}

static int64_t event_key(const feed_event_t *e, int tz_min)
{
    return e->all_day ? (int64_t)e->start_day * 86400 - 1 : e->start + (int64_t)tz_min * 60;
}

/* The hub's weight (§5) as the key's leading part: a higher weight sorts first, equal
 * weights fall through to the rest of the key; 0..12. */
static int64_t weight_rank(int w)
{
    return (int64_t)(FEED_WEIGHT_MAX - w);
}

/* DO FIRST to-dos by (-w, due date, feed order), all before today's events (keys >= 0). */
static int64_t first_todo_key(int w, int32_t day)
{
    return (weight_rank(w) - (FEED_WEIGHT_MAX - FEED_WEIGHT_MIN + 1)) * ((int64_t)1 << 40) + todo_key(day);
}

/* COMING UP: (-w, date, time); events have weight 0. */
static int64_t coming_key(int w, int64_t base)
{
    return weight_rank(w) * ((int64_t)1 << 40) + base;
}

/* ALSO: pinned by (-w, feed order), then undated by (-w, feed order); seq < 2^32. */
static int64_t also_key(bool pinned, int w, int32_t seq)
{
    return (pinned ? 0 : (int64_t)1 << 40) + weight_rank(w) * ((int64_t)1 << 32) + seq;
}

static today_item_t todo_item(int note, int item)
{
    return (today_item_t){.ref = {.kind = FEED_REF_TODO, .note = (int16_t)note, .item = (int16_t)item, .event = -1}};
}

static today_item_t event_item(int ev)
{
    return (today_item_t){.ref = {.kind = FEED_REF_EVENT, .note = -1, .item = -1, .event = (int16_t)ev}};
}

void today_plan_build(const feed_t *f, int32_t today, int64_t now, int tz_min, today_plan_t *out)
{
    memset(out, 0, sizeof(*out));
    pick_t first = {.cap = TODAY_PLAN_CAP}, coming = {.cap = TODAY_PLAN_CAP}, also = {.cap = TODAY_PLAN_CAP};
    int32_t seq = 0; /* ALSO keeps the feed's order within a weight: pinned notes first, then the rest */
    for (int n = 0; n < f->n_todos; n++) {
        const feed_todo_t *note = &f->todos[n];
        const bool dated = note->due_day != FEED_NO_DAY;
        if (!dated && today_is_shopping_list(note->title)) {
            continue;
        }
        const int w = feed_note_weight(f, note->id);
        for (int k = 0; k < note->n_items; k++) {
            if (note->items[k].done) {
                continue;
            }
            today_item_t it = todo_item(n, k);
            if (dated && note->due_day < today) {
                snprintf(it.reason, sizeof(it.reason), "overdue since %s", note->due);
                offer(&first, first_todo_key(w, note->due_day), &it);
            } else if (dated && note->due_day == today) {
                snprintf(it.reason, sizeof(it.reason), "due today");
                offer(&first, first_todo_key(w, today), &it);
            } else if (dated && note->due_day <= today + 7) {
                char d[24];
                pda_fmt_day(note->due_day, d, sizeof(d));
                snprintf(it.reason, sizeof(it.reason), "due %s", d);
                offer(&coming, coming_key(w, todo_key(note->due_day)), &it);
            } else if (note->pinned) {
                snprintf(it.reason, sizeof(it.reason), "pinned");
                offer(&also, also_key(true, w, seq++), &it);
            } else if (!dated) {
                snprintf(it.reason, sizeof(it.reason), "no date");
                offer(&also, also_key(false, w, seq++), &it);
            }
            /* unpinned, due after next week: not on Today */
        }
    }
    for (int i = 0; i < f->n_events; i++) {
        const feed_event_t *e = &f->events[i];
        const int32_t day = today_event_day(e, tz_min);
        if (today_event_over(e, today, now) || day < today || day > today + 7) {
            continue;
        }
        today_item_t it = event_item(i);
        today_event_when(e, today, tz_min, it.reason, sizeof(it.reason));
        offer(day == today ? &first : &coming, day == today ? event_key(e, tz_min) : coming_key(0, event_key(e, tz_min)),
              &it);
    }
    take(&out->sec[TODAY_DO_FIRST], &first);
    take(&out->sec[TODAY_COMING_UP], &coming);
    take(&out->sec[TODAY_ALSO], &also);
}

static bool same_ref(const feed_insight_item_t *a, const feed_insight_item_t *b)
{
    if (a->kind != b->kind) {
        return false;
    }
    return a->kind == FEED_REF_EVENT ? a->event == b->event : a->note == b->note && a->item == b->item;
}

static void push(today_section_t *s, const today_item_t *it)
{
    if (s->n < TODAY_PLAN_MAX) {
        s->items[s->n++] = *it;
    } else {
        s->more++;
        s->more_events += it->ref.kind == FEED_REF_EVENT;
    }
}

bool today_plan_from_insight(const feed_t *f, int32_t today, int64_t now, int tz_min, today_plan_t *out)
{
    if (!f->has_insight || f->insight_day != today) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    out->from_insight = true;
    out->headline = f->insight_headline;
    /* the hub's items, each once; today's timed events are set aside for the end of DO FIRST */
    const feed_insight_item_t *seen[FEED_INSIGHT_SECTIONS * FEED_MAX_INSIGHT_ITEMS];
    int n_seen = 0;
    const char *event_why[FEED_MAX_EVENTS] = {0};
    bool event_named[FEED_MAX_EVENTS] = {0};
    for (int s = 0; s < FEED_INSIGHT_SECTIONS; s++) {
        const feed_insight_list_t *l = &f->insight[s];
        for (int k = 0; k < l->n; k++) {
            const feed_insight_item_t *r = &l->items[k];
            bool dup = false;
            for (int j = 0; j < n_seen && !dup; j++) {
                dup = same_ref(seen[j], r);
            }
            if (dup) {
                continue;
            }
            seen[n_seen++] = r;
            today_item_t it = {.ref = *r};
            if (r->kind == FEED_REF_TODO) {
                if (r->note < 0 || r->note >= f->n_todos || r->item < 0 ||
                    r->item >= f->todos[r->note].n_items || f->todos[r->note].items[r->item].done) {
                    continue;
                }
            } else {
                if (r->event < 0 || r->event >= f->n_events) {
                    continue;
                }
                const feed_event_t *e = &f->events[r->event];
                if (today_event_over(e, today, now)) {
                    continue;
                }
                if (!e->all_day && today_event_day(e, tz_min) == today) {
                    event_named[r->event] = true;
                    event_why[r->event] = r->why;
                    continue;
                }
                today_event_when(e, today, tz_min, it.reason, sizeof(it.reason));
            }
            push(&out->sec[s], &it);
        }
    }
    /* today's timed events still to come, in time order (the events are sorted) */
    for (int i = 0; i < f->n_events; i++) {
        const feed_event_t *e = &f->events[i];
        if (e->all_day || today_event_day(e, tz_min) != today || today_event_over(e, today, now)) {
            continue;
        }
        today_item_t it = event_item(i);
        it.ref.why = event_named[i] ? event_why[i] : NULL;
        today_event_when(e, today, tz_min, it.reason, sizeof(it.reason));
        push(&out->sec[TODAY_DO_FIRST], &it);
    }
    return true;
}

void today_plan_for_today(const feed_t *f, int32_t today, int64_t now, int tz_min, today_plan_t *out)
{
    if (!today_plan_from_insight(f, today, now, tz_min, out)) {
        today_plan_build(f, today, now, tz_min, out);
    }
}
