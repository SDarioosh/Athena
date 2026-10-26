#include "ui_today.h"

#include <stdio.h>
#include <string.h>

#include "gfx.h"
#include "pda_time.h"
#include "ui_chrome.h"
#include "ui_layout.h"

#define HEAD_H UI_HEADING_H
#define ITEM_H (2 * LH + 12)  /* an item: its title, then the reason, a hairline between */
#define COL_W 88              /* the left column: a tick box, or an event's time */
#define ITEM_X (MARGIN + COL_W)
#define ITEM_W (W - MARGIN - ITEM_X)
#define HEADLINE_LINES 3
#define TICK_R 14
#define EMPTY_H 270           /* the "All caught up!" block */

static const char *const LABELS[TODAY_SECTIONS] = {"DO FIRST", "COMING UP", "ALSO WORTH A LOOK"};

typedef struct {
    const ui_today_in_t *in;
    int want;       /* the page to draw; -1: only count */
    int page, y;    /* where the next block goes */
} lay_t;

static void hit(const lay_t *l, EpdRect r, ui_today_hit_t kind, int32_t arg)
{
    if (l->in->hit) {
        l->in->hit(l->in->ctx, r, kind, arg);
    }
}

/* Room for a block of h px (on a new page if it doesn't fit): its top if it is on the
 * page being drawn, else -1. */
static int place(lay_t *l, int h)
{
    if (l->y + h > LIST_BOTTOM && l->y > TOP) {
        l->page++;
        l->y = TOP;
    }
    const int top = l->y;
    l->y += h;
    return l->page == l->want ? top : -1;
}

/* "Wed" from pda_fmt_day's "Wed 21 Oct": the events on Today are within a week. */
static void short_day(int32_t day, char *out, size_t cap)
{
    pda_fmt_day(day, out, cap);
    char *sp = strchr(out, ' ');
    if (sp) {
        *sp = 0;
    }
}

/* s without spaces and dots (".", "…") at its ends: its start, *len its length */
static const char *trimmed(const char *s, size_t *len)
{
    while (*s == ' ') {
        s++;
    }
    size_t n = strlen(s);
    for (;;) {
        if (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '.')) {
            n--;
        } else if (n >= 3 && memcmp(s + n - 3, "\xe2\x80\xa6", 3) == 0) {
            n -= 3;
        } else {
            break;
        }
    }
    *len = n;
    return s;
}

/* A second line that only repeats the first: the same words, any case, or the first line
 * cut short (a note's title is the hub's short form of its one item). */
static bool repeats(const char *line, const char *first)
{
    size_t nl, nf;
    line = trimmed(line, &nl);
    first = trimmed(first, &nf);
    if (nl == 0 || nl > nf) {
        return false;
    }
    for (size_t i = 0; i < nl; i++) {
        char a = line[i], b = first[i];
        a = (char)(a >= 'A' && a <= 'Z' ? a + 32 : a);
        b = (char)(b >= 'A' && b <= 'Z' ? b + 32 : b);
        if (a != b) {
            return false;
        }
    }
    return nl == nf || nl >= 20;
}

/* an item: first = right under a heading (no rule above it) */
static void draw_item(const lay_t *l, int top, const today_item_t *it, bool first)
{
    const feed_t *f = l->in->f;
    const int y1 = top + 33, y2 = y1 + LH;
    if (!first) {
        gfx_hline(ITEM_X, top + 3, W - MARGIN - ITEM_X, 0x00); /* a hairline between items */
    }
    char why[400]; /* the hub's why is up to 80 characters of UTF-8 */
    const char *w = it->ref.why;
    if (it->ref.kind == FEED_REF_TODO) {
        const char *r = it->reason;
        if (r[0] && w) {
            snprintf(why, sizeof(why), "%s \xc2\xb7 %s", r, w); /* · */
        } else {
            snprintf(why, sizeof(why), "%s", r[0] ? r : w ? w : "");
        }
        const feed_todo_item_t *ti = &f->todos[it->ref.note].items[it->ref.item];
        ui_today_todo_t st = {.done = ti->done};
        if (l->in->todo_state) {
            l->in->todo_state(l->in->ctx, it->ref.note, it->ref.item, &st);
        }
        gfx_check(MARGIN + 2 + TICK_R, y1 - 8, TICK_R, st.done);
        const int x_end = gfx_text_fit(TEXT_B, ITEM_X, y1, ITEM_W, ti->text, GFX_INK);
        if (st.done) {
            gfx_strike(TEXT_B, ITEM_X, y1, x_end - ITEM_X);
        }
        if (st.failed) {
            snprintf(why, sizeof(why), "couldn't sync \xe2\x80\x93 changed on the website?");
        } else if (!why[0]) {
            /* no reason given: the list it is on */
            const char *title = f->todos[it->ref.note].title;
            snprintf(why, sizeof(why), "%s", title ? title : "Untitled checklist");
        }
        if (!st.failed && repeats(why, ti->text)) {
            why[0] = 0; /* the to-do shows once */
        }
        gfx_text_fit(TEXT, ITEM_X, y2, ITEM_W, why, GFX_INK);
        hit(l, (EpdRect){0, top, W, ITEM_H}, UI_TODAY_HIT_TODO, it->ref.note * 64 + it->ref.item);
        return;
    }
    const feed_event_t *e = &f->events[it->ref.event];
    /* events: the time in the left column (the weekday under it when not today), "›"
     * opens the event; the second line is the hub's why and the place */
    char hm[16];
    if (e->all_day) {
        snprintf(hm, sizeof(hm), "all day");
    } else {
        pda_fmt_hm(e->start, l->in->tz_min, hm, sizeof(hm));
    }
    gfx_text_fit(TEXT_B, MARGIN, y1, COL_W - 8, hm, GFX_INK);
    const int32_t day = today_event_day(e, l->in->tz_min);
    if (day != l->in->today) {
        char d[24];
        short_day(day, d, sizeof(d));
        gfx_text_fit(TEXT, MARGIN, y2, COL_W - 8, d, GFX_INK);
    }
    const int aw = gfx_text_width(TEXT_B, "\xe2\x80\xba") + 8;
    gfx_text_fit(TEXT_B, ITEM_X, y1, ITEM_W - aw, e->title, GFX_INK);
    gfx_text_right(TEXT_B, W - MARGIN, y1, "\xe2\x80\xba", GFX_INK); /* › */
    why[0] = 0;
    if (w) {
        snprintf(why, sizeof(why), "%s", w);
    }
    if (e->where && strlen(why) + strlen(e->where) + 8 < sizeof(why)) {
        const size_t n = strlen(why);
        snprintf(why + n, sizeof(why) - n, "%s%s", n ? " \xc2\xb7 " : "", e->where);
    }
    if (!why[0] && !e->all_day && e->end > e->start) {
        char end[12];
        pda_fmt_hm(e->end, l->in->tz_min, end, sizeof(end));
        snprintf(why, sizeof(why), "until %s", end);
    }
    gfx_text_fit(TEXT, ITEM_X, y2, ITEM_W, why, GFX_INK);
    hit(l, (EpdRect){0, top, W, ITEM_H}, UI_TODAY_HIT_EVENT, it->ref.event);
}

static void centred(const EpdFont *f, int y, const char *s)
{
    gfx_text(f, (W - gfx_text_width(f, s)) / 2, y, s, GFX_INK);
}

/* Nothing to do: a big tick and "All caught up!" in the middle of the page. */
static void draw_empty(int top)
{
    const int cy = top + 70;
    gfx_check(W / 2, cy, 40, true);
    centred(HUGE, cy + 40 + 80, "All caught up!");
    centred(TEXT, cy + 40 + 130, "Nothing due and nothing on the calendar");
    centred(TEXT, cy + 40 + 130 + LH, "for the next 7 days.");
}

/* One pass over the content: counts pages (want -1) or draws page `want`. */
static int layout(const ui_today_in_t *in, int want)
{
    const today_plan_t *p = in->plan;
    lay_t l = {.in = in, .want = want, .page = 0, .y = TOP};
    bool any = false;
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        any |= p->sec[s].n > 0;
    }
    /* INSIGHT: the hub's headline, or "All caught up!" when there is none for today (left
     * out when there is nothing at all: the big "All caught up!" says it then) */
    const char *headline = p->from_insight ? p->headline : NULL;
    if ((!p->from_insight && any) || headline) {
        int lines = 1;
        if (headline) {
            lines = gfx_wrap(TEXT, 0, -1000, LIST_W, headline, GFX_INK, 1 << 20, 0);
            lines = lines < 1 ? 1 : lines > HEADLINE_LINES ? HEADLINE_LINES : lines;
        }
        const int top = place(&l, HEAD_H + lines * LH + 8);
        if (top >= 0) {
            ui_chrome_heading(top, "INSIGHT", NULL);
            const int y = top + HEAD_H + 24;
            if (headline) {
                gfx_wrap(TEXT, MARGIN, y, LIST_W, headline, GFX_INK, 0, HEADLINE_LINES);
            } else {
                gfx_check(MARGIN + 11, y - 8, 11, true);
                gfx_text_fit(TEXT_B, MARGIN + 34, y, LIST_W - 34, "All caught up!", GFX_INK);
            }
        }
    }
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        const today_section_t *sec = &p->sec[s];
        if (sec->n == 0) {
            continue;
        }
        /* the heading stays with the first item */
        int top = place(&l, HEAD_H + ITEM_H);
        const int page0 = l.page;
        if (top >= 0) {
            char more[32] = "";
            if (sec->more > 0) {
                snprintf(more, sizeof(more), "+%d more \xe2\x80\xba", sec->more);
            }
            ui_chrome_heading(top, LABELS[s], more);
            if (more[0]) {
                /* where most of the rest is */
                hit(&l, (EpdRect){W / 2, top, W / 2, HEAD_H},
                    2 * sec->more_events >= sec->more ? UI_TODAY_HIT_CALENDAR : UI_TODAY_HIT_TASKS, 0);
            }
            draw_item(&l, top + HEAD_H, &sec->items[0], true);
        }
        int cont_page = page0;
        for (int i = 1; i < sec->n; i++) {
            if (l.y + ITEM_H > LIST_BOTTOM) {
                /* the section goes on on the next page, under its heading again */
                top = place(&l, HEAD_H + ITEM_H);
            } else {
                top = place(&l, ITEM_H);
            }
            bool first = false;
            if (l.page != cont_page) {
                cont_page = l.page;
                first = true;
                if (top >= 0) {
                    char label[40];
                    snprintf(label, sizeof(label), "%s (cont.)", LABELS[s]);
                    ui_chrome_heading(top, label, NULL);
                    top += HEAD_H;
                }
            }
            if (top >= 0) {
                draw_item(&l, top, &sec->items[i], first);
            }
        }
    }
    if (!any) {
        const int top = place(&l, EMPTY_H);
        if (top >= 0) {
            draw_empty(top);
        }
    }
    return l.page + 1;
}

int ui_today_draw(const ui_today_in_t *in, int *page)
{
    const int pages = layout(in, -1);
    if (*page >= pages) {
        *page = pages - 1;
    }
    if (*page < 0) {
        *page = 0;
    }
    layout(in, *page);
    /* the digest itself is on Brief; the bottom row says it is there */
    if (in->f->has_briefing) {
        const char *s = "Digest ready on Brief \xe2\x80\xba";
        gfx_text(TEXT_B, MARGIN, ROW_Y + 33, s, GFX_INK);
        if (in->hit) {
            in->hit(in->ctx, (EpdRect){0, ROW_Y - 4, MARGIN + gfx_text_width(TEXT_B, s) + 24, ROW_H + 4},
                    UI_TODAY_HIT_BRIEF, 0);
        }
    }
    return pages;
}
