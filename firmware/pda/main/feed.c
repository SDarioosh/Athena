#include "feed.h"

#include <stdlib.h>
#include <string.h>

#include "json.h"
#include "pda_time.h"

typedef struct {
    json_doc_t d;
    char *arena;
    size_t used, cap;
} ctx_t;

/* Copy a string value into the arena; NULL if missing or not a string. */
static const char *str(ctx_t *c, int i)
{
    const int n = json_str_len(&c->d, i);
    if (n < 0 || c->used + (size_t)n + 1 > c->cap) {
        return NULL;
    }
    char *s = c->arena + c->used;
    json_str(&c->d, i, s, (size_t)n + 1);
    c->used += (size_t)n + 1;
    return s;
}

static const char *get_str(ctx_t *c, int obj, const char *key)
{
    return str(c, json_get(&c->d, obj, key));
}

static int str_list(ctx_t *c, int obj, const char *key, const char **out, int max)
{
    const int arr = json_get(&c->d, obj, key);
    int n = 0;
    for (int i = json_first(&c->d, arr); i >= 0 && n < max; i = json_next(&c->d, arr, i)) {
        const char *s = str(c, i);
        if (s) {
            out[n++] = s;
        }
    }
    return n;
}

static bool get_time(ctx_t *c, int obj, const char *key, int64_t *t, bool *date_only)
{
    char buf[40];
    const int i = json_get(&c->d, obj, key);
    if (json_str(&c->d, i, buf, sizeof(buf)) < 0) {
        return false;
    }
    return pda_time_parse(buf, t, date_only);
}

static int setup(ctx_t *c, const char *json, size_t len, json_tok_t **toks)
{
    const int max = (int)(len / 2 + 16);
    *toks = malloc(sizeof(json_tok_t) * (size_t)max);
    if (!*toks) {
        return FEED_E_NOMEM;
    }
    const int n = json_parse(json, len, *toks, max);
    if (n < 0 || (*toks)[0].type != JSON_OBJ) {
        free(*toks);
        return FEED_E_JSON;
    }
    c->d = (json_doc_t){.js = json, .t = *toks, .n = n};
    c->cap = len + (size_t)n + 16;
    c->arena = malloc(c->cap);
    c->used = 0;
    if (!c->arena) {
        free(*toks);
        return FEED_E_NOMEM;
    }
    int64_t v = 0;
    if (!json_int(&c->d, json_get(&c->d, 0, "v"), &v) || v != 1) {
        free(*toks);
        free(c->arena);
        return FEED_E_VERSION;
    }
    return 0;
}

static void parse_briefing(ctx_t *c, feed_t *f)
{
    const int b = json_get(&c->d, 0, "briefing");
    if (b < 0 || c->d.t[b].type != JSON_OBJ) {
        return;
    }
    f->has_briefing = true;
    f->brief_date = get_str(c, b, "date");
    f->brief_text = get_str(c, b, "text");
    f->n_highlights = str_list(c, b, "highlights", f->highlights, FEED_MAX_HIGHLIGHTS);
}

static void parse_conversations(ctx_t *c, feed_t *f)
{
    const int arr = json_get(&c->d, 0, "conversations");
    for (int o = json_first(&c->d, arr); o >= 0 && f->n_conv < FEED_MAX_CONV;
         o = json_next(&c->d, arr, o)) {
        if (c->d.t[o].type != JSON_OBJ) {
            continue;
        }
        feed_conv_t *cv = &f->conv[f->n_conv];
        *cv = (feed_conv_t){0};
        cv->id = get_str(c, o, "id");
        cv->title = get_str(c, o, "title");
        if (!cv->id || !cv->title) {
            continue;
        }
        get_time(c, o, "at", &cv->at, NULL);
        cv->summary = get_str(c, o, "summary");
        cv->n_people = str_list(c, o, "people", cv->people, FEED_MAX_PEOPLE);
        cv->n_points = str_list(c, o, "points", cv->points, FEED_MAX_POINTS);
        f->n_conv++;
    }
}

static void parse_actions(ctx_t *c, feed_t *f)
{
    const int arr = json_get(&c->d, 0, "actions");
    for (int o = json_first(&c->d, arr); o >= 0 && f->n_actions < FEED_MAX_ACTIONS;
         o = json_next(&c->d, arr, o)) {
        if (c->d.t[o].type != JSON_OBJ) {
            continue;
        }
        feed_action_t *a = &f->actions[f->n_actions];
        *a = (feed_action_t){.due_day = FEED_NO_DAY};
        if (!json_int(&c->d, json_get(&c->d, o, "id"), &a->id)) {
            continue;
        }
        a->text = get_str(c, o, "text");
        if (!a->text) {
            continue;
        }
        a->owner = get_str(c, o, "owner");
        a->conv = get_str(c, o, "conv");
        int64_t t;
        bool date_only;
        if (get_time(c, o, "due", &t, &date_only)) {
            a->due_day = pda_local_day(t, 0);
            pda_fmt_ymd(a->due_day, a->due, sizeof(a->due));
        }
        f->n_actions++;
    }
}

static void parse_calendar(ctx_t *c, feed_t *f)
{
    const int arr = json_get(&c->d, 0, "calendar");
    for (int o = json_first(&c->d, arr); o >= 0 && f->n_events < FEED_MAX_EVENTS;
         o = json_next(&c->d, arr, o)) {
        if (c->d.t[o].type != JSON_OBJ) {
            continue;
        }
        feed_event_t *e = &f->events[f->n_events];
        *e = (feed_event_t){.start_day = FEED_NO_DAY};
        bool date_only = false;
        e->title = get_str(c, o, "title");
        if (!e->title || !get_time(c, o, "start", &e->start, &date_only)) {
            continue;
        }
        e->id = get_str(c, o, "id");
        e->where = get_str(c, o, "where");
        json_bool(&c->d, json_get(&c->d, o, "all_day"), &e->all_day);
        e->all_day |= date_only;
        if (!get_time(c, o, "end", &e->end, NULL)) {
            e->end = e->start;
        }
        if (e->all_day) {
            /* A date (or a midnight) stands for that calendar day, not a UTC instant. */
            e->start_day = pda_local_day(e->start, 0);
        }
        f->n_events++;
    }
}

int feed_parse(const char *json, size_t len, feed_t *f)
{
    memset(f, 0, sizeof(*f));
    ctx_t c;
    json_tok_t *toks;
    int rc = setup(&c, json, len, &toks);
    if (rc != 0) {
        return rc;
    }
    int64_t version = 0;
    if (!json_int(&c.d, json_get(&c.d, 0, "version"), &version) || version < 0 ||
        version > 0xFFFFFFFFLL) {
        free(toks);
        free(c.arena);
        return FEED_E_FIELD;
    }
    f->version = (uint32_t)version;
    get_time(&c, 0, "generated", &f->generated, NULL);
    f->tz = get_str(&c, 0, "tz");
    parse_briefing(&c, f);
    parse_conversations(&c, f);
    parse_actions(&c, f);
    parse_calendar(&c, f);
    free(toks);
    f->arena = c.arena;
    feed_sort_actions(f);
    feed_sort_events(f);
    return 0;
}

void feed_free(feed_t *f)
{
    free(f->arena);
    f->arena = NULL;
}

static int cmp_action(const void *pa, const void *pb)
{
    const feed_action_t *a = pa, *b = pb;
    if (a->due_day != b->due_day) {
        if (a->due_day == FEED_NO_DAY) {
            return 1;
        }
        if (b->due_day == FEED_NO_DAY) {
            return -1;
        }
        return a->due_day < b->due_day ? -1 : 1;
    }
    return a->id < b->id ? -1 : a->id > b->id;
}

void feed_sort_actions(feed_t *f)
{
    qsort(f->actions, (size_t)f->n_actions, sizeof(f->actions[0]), cmp_action);
}

static int cmp_event(const void *pa, const void *pb)
{
    const feed_event_t *a = pa, *b = pb;
    /* all-day events sort at the start of their day */
    const int64_t ka = a->all_day ? (int64_t)a->start_day * 86400 - 1 : a->start;
    const int64_t kb = b->all_day ? (int64_t)b->start_day * 86400 - 1 : b->start;
    return ka < kb ? -1 : ka > kb;
}

void feed_sort_events(feed_t *f)
{
    qsort(f->events, (size_t)f->n_events, sizeof(f->events[0]), cmp_event);
}

/* ---- details ---- */

int detail_parse(const char *json, size_t len, detail_t *d)
{
    memset(d, 0, sizeof(*d));
    ctx_t c;
    json_tok_t *toks;
    int rc = setup(&c, json, len, &toks);
    if (rc != 0) {
        return rc;
    }
    d->ref = get_str(&c, 0, "ref");
    if (!d->ref) {
        free(toks);
        free(c.arena);
        return FEED_E_FIELD;
    }
    d->title = get_str(&c, 0, "title");
    d->summary = get_str(&c, 0, "summary");
    d->n_points = str_list(&c, 0, "points", d->points, FEED_MAX_POINTS);
    json_bool(&c.d, json_get(&c.d, 0, "more"), &d->more);
    const int arr = json_get(&c.d, 0, "segments");
    for (int o = json_first(&c.d, arr); o >= 0 && d->n_seg < DETAIL_MAX_SEGMENTS;
         o = json_next(&c.d, arr, o)) {
        if (c.d.t[o].type != JSON_OBJ) {
            continue;
        }
        detail_seg_t *s = &d->seg[d->n_seg];
        *s = (detail_seg_t){0};
        s->x = get_str(&c, o, "x");
        if (!s->x) {
            continue;
        }
        json_num(&c.d, json_get(&c.d, o, "o"), &s->o);
        s->s = get_str(&c, o, "s");
        json_bool(&c.d, json_get(&c.d, o, "me"), &s->me);
        d->n_seg++;
    }
    free(toks);
    d->arena = c.arena;
    return 0;
}

void detail_free(detail_t *d)
{
    free(d->arena);
    d->arena = NULL;
}

bool detail_peek_ref(const char *json, size_t len, char *ref, size_t cap)
{
    detail_t *d = malloc(sizeof(*d));
    if (!d) {
        return false;
    }
    const bool ok = detail_parse(json, len, d) == 0;
    if (ok) {
        strncpy(ref, d->ref, cap - 1);
        ref[cap - 1] = 0;
        detail_free(d);
    }
    free(d);
    return ok;
}
