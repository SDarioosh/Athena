#include "ui_today.h"

#include <stdio.h>
#include <string.h>

#include "gfx.h"
#include "ui_layout.h"

#define HEAD_H 44             /* a section heading: its label's baseline 28 px down */
#define ITEM_H (2 * LH + 8)   /* an item: its text, then the reason */
#define ITEM_X (MARGIN + 44)  /* item text, right of the tick box */
#define ITEM_W (W - MARGIN - ITEM_X)
#define HEADLINE_LINES 3
#define BOX 28

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

/* A caps label with a 2 px rule to the right edge, or to a note at the right end (all
 * black: the panel doesn't show grey). */
static void heading(int top, const char *label, const char *right)
{
    const int y = top + 28;
    const int x = gfx_text(TEXT, MARGIN, y, label, GFX_INK) + 12;
    int x_end = W - MARGIN;
    if (right && *right) {
        const int rw = gfx_text_width(TEXT, right);
        gfx_text(TEXT, x_end - rw, y, right, GFX_INK);
        x_end -= rw + 12;
    }
    if (x_end > x) {
        gfx_hline(x, y - 9, x_end - x, 0x00);
        gfx_hline(x, y - 8, x_end - x, 0x00);
    }
}

static void draw_item(const lay_t *l, int top, const today_item_t *it)
{
    const feed_t *f = l->in->f;
    const int y1 = top + 26, y2 = y1 + LH;
    char why[400]; /* the hub's why is up to 80 characters of UTF-8 */
    const char *r = it->reason, *w = it->ref.why;
    if (r[0] && w) {
        snprintf(why, sizeof(why), "%s \xc2\xb7 %s", r, w); /* · */
    } else {
        snprintf(why, sizeof(why), "%s", r[0] ? r : w ? w : "");
    }
    if (it->ref.kind == FEED_REF_TODO) {
        const feed_todo_item_t *ti = &f->todos[it->ref.note].items[it->ref.item];
        ui_today_todo_t st = {.done = ti->done};
        if (l->in->todo_state) {
            l->in->todo_state(l->in->ctx, it->ref.note, it->ref.item, &st);
        }
        gfx_frame((EpdRect){MARGIN + 4, y1 - 22, BOX, BOX}, 0x00);
        if (st.done) {
            gfx_text(TEXT, MARGIN + 9, y1 + 1, "\xe2\x9c\x93", GFX_INK); /* ✓ */
        }
        const int x_end = gfx_text_fit(TEXT, ITEM_X, y1, ITEM_W, ti->text, GFX_INK);
        if (st.done) {
            gfx_strike(TEXT, ITEM_X, y1, x_end - ITEM_X);
        }
        if (st.failed) {
            snprintf(why, sizeof(why), "couldn't sync \xe2\x80\x93 changed on the website?");
        } else if (!why[0]) {
            /* no reason given: the list it is on */
            const char *title = f->todos[it->ref.note].title;
            snprintf(why, sizeof(why), "%s", title ? title : "Untitled checklist");
        }
        gfx_text_fit(TEXT, ITEM_X, y2, ITEM_W, why, GFX_INK);
        hit(l, (EpdRect){0, top, W, ITEM_H}, UI_TODAY_HIT_TODO, it->ref.note * 64 + it->ref.item);
        return;
    }
    const feed_event_t *e = &f->events[it->ref.event];
    /* events: a filled mark (not a box: nothing to tick), "›" opens the event */
    gfx_fill((EpdRect){MARGIN + 12, y1 - 14, 12, 12}, 0x00);
    const int aw = gfx_text_width(TEXT, "\xe2\x80\xba") + 8;
    gfx_text_fit(TEXT, ITEM_X, y1, ITEM_W - aw, e->title, GFX_INK);
    gfx_text_right(TEXT, W - MARGIN, y1, "\xe2\x80\xba", GFX_INK); /* › */
    if (e->where && strlen(why) + strlen(e->where) + 8 < sizeof(why)) {
        const size_t n = strlen(why);
        snprintf(why + n, sizeof(why) - n, "%s%s", n ? " \xc2\xb7 " : "", e->where);
    }
    gfx_text_fit(TEXT, ITEM_X, y2, ITEM_W, why, GFX_INK);
    hit(l, (EpdRect){0, top, W, ITEM_H}, UI_TODAY_HIT_EVENT, it->ref.event);
}

/* One pass over the content: counts pages (want -1) or draws page `want`. */
static int layout(const ui_today_in_t *in, int want)
{
    const today_plan_t *p = in->plan;
    lay_t l = {.in = in, .want = want, .page = 0, .y = TOP};
    /* INSIGHT: the hub's headline, or one line saying there is none for today */
    const char *headline = p->from_insight ? p->headline : NULL;
    if (!p->from_insight || headline) {
        int lines = 1;
        if (headline) {
            lines = gfx_wrap(TEXT, 0, -1000, LIST_W, headline, GFX_INK, 1 << 20, 0);
            lines = lines < 1 ? 1 : lines > HEADLINE_LINES ? HEADLINE_LINES : lines;
        }
        const int top = place(&l, HEAD_H + lines * LH + 8);
        if (top >= 0) {
            heading(top, "INSIGHT", NULL);
            const int y = top + HEAD_H + 24;
            if (headline) {
                gfx_wrap(TEXT, MARGIN, y, LIST_W, headline, GFX_INK, 0, HEADLINE_LINES);
            } else {
                gfx_text_fit(TEXT, MARGIN, y, LIST_W, "No insight for today yet: by date and time.",
                             GFX_INK);
            }
        }
    }
    bool any = false;
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        const today_section_t *sec = &p->sec[s];
        if (sec->n == 0) {
            continue;
        }
        any = true;
        /* the heading stays with the first item */
        int top = place(&l, HEAD_H + ITEM_H);
        const int page0 = l.page;
        if (top >= 0) {
            char more[32] = "";
            if (sec->more > 0) {
                snprintf(more, sizeof(more), "+%d more \xe2\x80\xba", sec->more);
            }
            heading(top, LABELS[s], more);
            if (more[0]) {
                /* where most of the rest is */
                hit(&l, (EpdRect){W / 2, top, W / 2, HEAD_H},
                    2 * sec->more_events >= sec->more ? UI_TODAY_HIT_CALENDAR : UI_TODAY_HIT_TASKS, 0);
            }
            draw_item(&l, top + HEAD_H, &sec->items[0]);
        }
        int cont_page = page0;
        for (int i = 1; i < sec->n; i++) {
            if (l.y + ITEM_H > LIST_BOTTOM) {
                /* the section goes on on the next page, under its heading again */
                top = place(&l, HEAD_H + ITEM_H);
            } else {
                top = place(&l, ITEM_H);
            }
            if (l.page != cont_page) {
                cont_page = l.page;
                if (top >= 0) {
                    char label[40];
                    snprintf(label, sizeof(label), "%s (cont.)", LABELS[s]);
                    heading(top, label, NULL);
                    top += HEAD_H;
                }
            }
            if (top >= 0) {
                draw_item(&l, top, &sec->items[i]);
            }
        }
    }
    if (!any) {
        const int top = place(&l, LH + 12);
        if (top >= 0) {
            gfx_text_fit(TEXT, MARGIN, top + 30, LIST_W, "Nothing due and nothing on the calendar this week.",
                         GFX_INK);
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
        gfx_text(TEXT, MARGIN, ROW_Y + 33, s, GFX_INK);
        if (in->hit) {
            in->hit(in->ctx, (EpdRect){0, ROW_Y - 4, MARGIN + gfx_text_width(TEXT, s) + 24, ROW_H + 4},
                    UI_TODAY_HIT_BRIEF, 0);
        }
    }
    return pages;
}
