/* PDA screens (docs/t5-pda-plan.md §5). Portrait 540x960 (pda_epd's rotation): a
 * header (title and clock, then date, battery, sync), the content area, a row for the
 * screen's buttons and the pager, and a tab bar. Each redraw paints the whole
 * framebuffer and records tap regions; epdiy then drives only the pixels that changed.
 *
 * Refresh policy (§5): a new screen is a full (flashing) update, a change on the same
 * screen (a pending mark, a page) a partial one, the minute clock a small partial that
 * doesn't count towards the ghosting budget; every PDA_EPD_PARTIAL_LIMIT updates and the
 * side button give a clean refresh. */
#include "ui_pda.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ble_pda.h"
#include "crc32.h"
#include "esp_log.h"
#include "fonts.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gesture.h"
#include "gfx.h"
#include "pda_actions.h"
#include "pda_app.h"
#include "pda_epd.h"
#include "pda_time.h"
#include "t5_board.h"

static const char *TAG = "ui";

#define W PDA_SCR_W
#define H PDA_SCR_H
#define HEADER_H 100
#define TAB_H 76
#define TAB_Y (H - TAB_H)
#define TOP (HEADER_H + 8)
#define BOTTOM (TAB_Y - 6)
#define MARGIN 16
#define ROW_H 52                /* the bottom row: the screen's buttons and the pager */
#define ROW_Y (BOTTOM - ROW_H)
#define LIST_BOTTOM (ROW_Y - 10) /* lists and text end here */
#define LIST_W (W - 2 * MARGIN)
#define PAGER_BTN 64
#define TEXT (&FONT_TEXT)
#define BIG (&FONT_BIG)
#define LH 30                   /* FONT_TEXT line advance */
#define TRANSCRIPT_PAGE_S 600   /* §4.3: 10 minutes per page */
#define MAX_HITS 64
#define TASK_STACK 8192

static const char *const SCREEN_NAMES[UI_SCREEN_COUNT] = {
    "today", "briefing", "calendar", "actions", "talks", "status",
    "conversation", "transcript", "event", "templates", "new-event",
};
static const char *const TAB_LABELS[6] = {"Today", "Brief", "Cal", "Tasks", "Talks", "Status"};
static const char *const DEFAULT_TEMPLATES[] = {"Call", "Meeting", "Reminder"};

typedef enum {
    A_NONE,
    A_TAB,        /* arg: screen */
    A_OPEN,       /* arg: screen */
    A_PAGE,       /* arg: +1 / -1 */
    A_ITEM,       /* arg: action index in the feed */
    A_ITEM_CAL,   /* arg: action index */
    A_CONV,       /* arg: conversation index */
    A_EVENT,      /* arg: event index */
    A_BACK,
    A_DIGEST,
    A_TRANSCRIPT,
    A_NEXT_PAGE,
    A_PLUS,
    A_TEMPLATE,   /* arg: template index */
    A_DAY,        /* arg: +1 / -1 */
    A_SLOT,       /* arg: minute of the day */
    A_STEP,       /* arg: +-15 */
    A_DURATION,   /* arg: minutes, 0 = all day */
    A_ADD,
    A_PAIR,
    A_CLEAN,
    A_WIPE,       /* long press only */
    A_CANCEL_PENDING, /* arg: index into the pending list */
    A_ROTATE,     /* the other portrait orientation */
} action_t;

typedef struct {
    EpdRect r;
    uint8_t act;
    int32_t arg;
} hit_t;

static struct {
    ui_screen_t screen, back_to;
    int page, pages;          /* the current screen's page and page count */
    int sel;                  /* selected conversation / event */
    char transcript_ref[PDA_REF_MAX];
    detail_t *detail;         /* the loaded transcript page, or NULL */
    char toast[96];
    int32_t passkey;          /* -1: no pairing code on screen */
    /* new-event form */
    char ev_title[96];
    int32_t ev_day;
    int ev_min, ev_dur;       /* minute of the day; duration in minutes, 0 = all day */
    int64_t ev_from_item;
    hit_t hits[MAX_HITS];
    int n_hits;
    bool screen_changed;
} ui = {.passkey = -1};

const char *ui_screen_name(ui_screen_t s)
{
    return s < UI_SCREEN_COUNT ? SCREEN_NAMES[s] : "?";
}

int ui_screen_from_name(const char *name)
{
    for (int i = 0; i < UI_SCREEN_COUNT; i++) {
        if (strcmp(name, SCREEN_NAMES[i]) == 0) {
            return i;
        }
    }
    return -1;
}

static void hit(EpdRect r, action_t act, int32_t arg)
{
    if (ui.n_hits < MAX_HITS) {
        ui.hits[ui.n_hits++] = (hit_t){r, (uint8_t)act, arg};
    }
}

static void toast(const char *msg)
{
    snprintf(ui.toast, sizeof(ui.toast), "%s", msg);
}

static int64_t now_s(void)
{
    const feed_t *f = pda_app_feed();
    if (pda_app_time_valid()) {
        return pda_app_now();
    }
    return f ? f->generated : 0;
}

static int tz(void)
{
    return pda_app_tz_min();
}

static void go(ui_screen_t s)
{
    if (s != ui.screen) {
        /* Back from a detail screen returns to the list it was opened from. */
        if (s < UI_CONV_DETAIL) {
            ui.back_to = UI_TODAY;
        } else if (ui.screen < UI_CONV_DETAIL) {
            ui.back_to = ui.screen;
        }
        ui.screen = s;
        ui.page = 0;
        ui.screen_changed = true;
    }
}

/* ---- header, tabs, pager ---- */

static void draw_header(const char *title)
{
    gfx_fill((EpdRect){0, 0, W, HEADER_H}, 0xFF);
    char clock[16] = "--:--", date[24] = "";
    if (pda_app_time_valid()) {
        pda_fmt_date(pda_app_now(), tz(), date, sizeof(date));
        pda_fmt_hm(pda_app_now(), tz(), clock, sizeof(clock));
    }
    const int cw = gfx_text_width(BIG, clock);
    gfx_text_fit(BIG, MARGIN, 48, W - 2 * MARGIN - cw - 20, title, GFX_INK);
    gfx_text(BIG, W - MARGIN - cw, 48, clock, GFX_INK);
    t5_battery_t b;
    char batt[24] = "";
    if (t5_battery_read(&b) == ESP_OK && b.percent >= 0) {
        snprintf(batt, sizeof(batt), "%d%%%s", b.percent, b.charging ? "+" : "");
    }
    char sync[48];
    const int64_t last = ble_pda_last_sync();
    const feed_t *f = pda_app_feed();
    if (ble_pda_is_connected()) {
        snprintf(sync, sizeof(sync), "phone connected");
    } else if (last > 0 && pda_app_time_valid()) {
        char age[24];
        pda_fmt_age(pda_app_now() - last, age, sizeof(age));
        snprintf(sync, sizeof(sync), "synced %s", age);
    } else if (f && pda_app_time_valid() && f->generated > 0) {
        char age[24];
        pda_fmt_age(pda_app_now() - f->generated, age, sizeof(age));
        snprintf(sync, sizeof(sync), "feed %s", age);
    } else {
        snprintf(sync, sizeof(sync), f ? "feed loaded" : "no feed");
    }
    const int ob = pda_app_outbox_count();
    char line[160];
    snprintf(line, sizeof(line), "%s \xc2\xb7 %s \xc2\xb7 %s", date, batt, sync);
    if (ob > 0) {
        char obs[16];
        snprintf(obs, sizeof(obs), "\xe2\x86\x91%d", ob); /* ↑n: actions waiting for the phone */
        gfx_text_right(TEXT, W - MARGIN, 86, obs, GFX_INK);
        gfx_text_fit(TEXT, MARGIN, 86, W - 2 * MARGIN - 60, line, GFX_INK);
    } else {
        gfx_text_fit(TEXT, MARGIN, 86, W - 2 * MARGIN, line, GFX_INK);
    }
    gfx_hline(0, HEADER_H - 2, W, 0x00);
    gfx_hline(0, HEADER_H - 1, W, 0x00);
}

static EpdRect header_area(void)
{
    return (EpdRect){0, 0, W, HEADER_H};
}

static void draw_tabs(void)
{
    const int tw = W / 6;
    gfx_hline(0, TAB_Y, W, 0x00);
    ui_screen_t active = ui.screen;
    if (active >= UI_CONV_DETAIL) {
        active = active == UI_CONV_DETAIL || active == UI_TRANSCRIPT ? UI_CONVERSATIONS
                 : active == UI_EVENT || active == UI_TEMPLATES || active == UI_NEW_EVENT
                     ? UI_CALENDAR
                     : UI_TODAY;
    }
    for (int i = 0; i < 6; i++) {
        const EpdRect r = {i * tw + 4, TAB_Y + 6, tw - 8, TAB_H - 10};
        gfx_button(r, TAB_LABELS[i], (int)active == i);
        hit((EpdRect){i * tw, TAB_Y, tw, TAB_H}, A_TAB, i);
    }
}

static void draw_toast(void)
{
    if (!ui.toast[0]) {
        return;
    }
    const EpdRect r = {MARGIN, ROW_Y - 50, W - 2 * MARGIN, 44};
    gfx_fill(r, 0x00);
    EpdFontProperties p = epd_font_properties_default();
    p.fg_color = 0xF;
    p.bg_color = 0x0;
    p.fallback_glyph = '?';
    int x = r.x + 12, y = r.y + 31;
    epd_write_string(TEXT, ui.toast, &x, &y, pda_epd_fb(), &p);
    ui.toast[0] = 0; /* shown once */
}

/* ▲ n/m ▼ at the right end of the bottom row when there is more than one page. */
static void draw_pager(void)
{
    if (ui.pages <= 1) {
        return;
    }
    const EpdRect down = {W - MARGIN - PAGER_BTN, ROW_Y, PAGER_BTN, ROW_H - 4};
    const EpdRect up = {down.x - 70 - PAGER_BTN, ROW_Y, PAGER_BTN, ROW_H - 4};
    gfx_button(up, "\xe2\x96\xb2", false);   /* ▲ */
    gfx_button(down, "\xe2\x96\xbc", false); /* ▼ */
    char pg[16];
    snprintf(pg, sizeof(pg), "%d/%d", ui.page + 1, ui.pages);
    gfx_text(TEXT, up.x + PAGER_BTN + (70 - gfx_text_width(TEXT, pg)) / 2, ROW_Y + 33, pg, GFX_FAINT);
    hit(up, A_PAGE, -1);
    hit(down, A_PAGE, 1);
}

/* A button in the bottom row, from the left. */
static EpdRect row_button(int x, int w)
{
    return (EpdRect){x, ROW_Y, w, ROW_H - 4};
}

/* Text lines that fit between y0 (first baseline) and LIST_BOTTOM. */
static int lines_from(int y0)
{
    return (LIST_BOTTOM - y0) / LH + 1;
}

static void clamp_page(int pages)
{
    ui.pages = pages < 1 ? 1 : pages;
    if (ui.page >= ui.pages) {
        ui.page = ui.pages - 1;
    }
    if (ui.page < 0) {
        ui.page = 0;
    }
}

/* ---- paged text writer: paragraphs flow over pages of `per_page` lines ---- */

typedef struct {
    int x, y0, w, per_page;
    int line;   /* lines laid out so far */
    int first;  /* first line of the current page */
} pw_t;

static pw_t pw_begin(int x, int y0, int w, int per_page)
{
    return (pw_t){.x = x, .y0 = y0, .w = w, .per_page = per_page, .first = ui.page * per_page};
}

static void pw_para(pw_t *pw, const EpdFont *f, const char *s, uint8_t color, int indent)
{
    if (!s || !*s) {
        return;
    }
    const int lh = gfx_line(f) / LH > 1 ? 2 : 1; /* a big font line takes two text lines */
    const int start = pw->line;
    int skip = pw->first - start;
    int y_line = start - pw->first;
    if (skip < 0) {
        skip = 0;
    }
    if (y_line < 0) {
        y_line = 0;
    }
    const int room = pw->per_page - y_line;
    int n;
    if (room > 0 && lh == 1) {
        n = gfx_wrap(f, pw->x + indent, pw->y0 + y_line * LH, pw->w - indent, s, color, skip, room);
    } else if (room > 1 && skip == 0 && start >= pw->first) {
        gfx_text_fit(f, pw->x + indent, pw->y0 + y_line * LH + LH, pw->w - indent, s, color);
        n = 1;
    } else {
        /* off this page: only count */
        n = lh == 1 ? gfx_wrap(f, 0, -1000, pw->w - indent, s, color, 1 << 20, 0) : 1;
    }
    pw->line += n * lh;
}

static void pw_gap(pw_t *pw)
{
    if (pw->line > 0) {
        pw->line++;
    }
}

static int pw_pages(const pw_t *pw)
{
    return (pw->line + pw->per_page - 1) / pw->per_page;
}

/* ---- screens ---- */

static void draw_no_feed(void)
{
    int y = TOP + 60;
    gfx_text(BIG, MARGIN + 20, y, "No feed yet", GFX_INK);
    y += 60;
    gfx_wrap(TEXT, MARGIN + 20, y, W - 2 * MARGIN - 40,
             "Pair a phone on the Status screen (or hold BOOT for a second). The phone sends the "
             "briefing, conversations, action items and calendar when it syncs. For a test, load "
             "the demo feed over USB: tools/pda_serial.py put test/fixtures/feed_demo.json",
             GFX_INK, 0, 8);
    hit((EpdRect){0, TOP, W, BOTTOM - TOP}, A_TAB, UI_STATUS);
}

static void fmt_event_when(const feed_event_t *e, char *out, size_t cap, bool with_day)
{
    char d[24] = "", a[12], b[12];
    if (e->all_day) {
        if (with_day) {
            pda_fmt_day(e->start_day, d, sizeof(d));
        }
        snprintf(out, cap, "%s%sall day", d, with_day ? "  " : "");
        return;
    }
    if (with_day) {
        pda_fmt_date(e->start, tz(), d, sizeof(d));
    }
    pda_fmt_hm(e->start, tz(), a, sizeof(a));
    pda_fmt_hm(e->end, tz(), b, sizeof(b));
    if (e->end > e->start) {
        snprintf(out, cap, "%s%s%s\xe2\x80\x93%s", d, with_day ? "  " : "", a, b);
    } else {
        snprintf(out, cap, "%s%s%s", d, with_day ? "  " : "", a);
    }
}

/* Events that haven't ended yet (all-day: from today on). */
static bool event_upcoming(const feed_event_t *e, int64_t now)
{
    if (e->all_day) {
        return e->start_day >= pda_local_day(now, tz());
    }
    return (e->end > e->start ? e->end : e->start + 1) > now;
}

static void draw_today(const feed_t *f)
{
    draw_header("Today");
    if (!f) {
        draw_no_feed();
        return;
    }
    const int64_t now = now_s();
    char buf[96];
    /* the next three events */
    int y = TOP + 26;
    const int y_ev = TOP;
    gfx_text(TEXT, MARGIN, y, "NEXT", GFX_FAINT);
    y += LH + 4;
    int shown = 0;
    for (int i = 0; i < f->n_events && shown < 3; i++) {
        const feed_event_t *e = &f->events[i];
        if (!event_upcoming(e, now)) {
            continue;
        }
        char when[48];
        fmt_event_when(e, when, sizeof(when), true);
        gfx_text(TEXT, MARGIN, y, when, GFX_INK);
        y += LH;
        gfx_text_fit(TEXT, MARGIN + 20, y, LIST_W - 20, e->title, GFX_INK);
        y += LH;
        if (e->where) {
            gfx_text_fit(TEXT, MARGIN + 20, y, LIST_W - 20, e->where, GFX_FAINT);
            y += LH;
        }
        y += 8;
        shown++;
    }
    if (!shown) {
        gfx_text(TEXT, MARGIN, y, "Nothing in the next 7 days", GFX_FAINT);
        y += LH;
    }
    hit((EpdRect){0, y_ev, W, y - y_ev - 10}, A_OPEN, UI_CALENDAR);
    gfx_hline(MARGIN, y - 14, LIST_W, 0xA0);

    /* the briefing's first lines */
    y += 16;
    if (f->has_briefing) {
        const int y_b = y - 30;
        snprintf(buf, sizeof(buf), "BRIEFING %s", f->brief_date ? f->brief_date : "");
        gfx_text(TEXT, MARGIN, y, buf, GFX_FAINT);
        y += LH + 4;
        const int n = gfx_wrap(TEXT, MARGIN, y, LIST_W, f->brief_text ? f->brief_text : "", GFX_INK, 0, 7);
        y += (n < 7 ? n : 7) * LH + 8;
        hit((EpdRect){0, y_b, W, y - y_b - 10}, A_OPEN, UI_BRIEFING);
        gfx_hline(MARGIN, y - 14, LIST_W, 0xA0);
        y += 16;
    }

    /* open actions */
    int due_week = 0;
    const int32_t today = pda_local_day(now, tz());
    for (int i = 0; i < f->n_actions; i++) {
        if (f->actions[i].due_day != FEED_NO_DAY && f->actions[i].due_day <= today + 7) {
            due_week++;
        }
    }
    const int y_a = y - 30;
    snprintf(buf, sizeof(buf), "%d open action item%s", f->n_actions, f->n_actions == 1 ? "" : "s");
    gfx_text(BIG, MARGIN, y + 14, buf, GFX_INK);
    y += 50;
    if (due_week) {
        snprintf(buf, sizeof(buf), "%d due within a week", due_week);
        gfx_text(TEXT, MARGIN, y, buf, GFX_FAINT);
        y += LH;
    }
    hit((EpdRect){0, y_a, W, y - y_a}, A_OPEN, UI_ACTIONS);

    /* the latest conversation */
    if (f->n_conv > 0 && y + 2 * LH + 20 < BOTTOM) {
        y += 16;
        gfx_text(TEXT, MARGIN, y, "LATEST CONVERSATION", GFX_FAINT);
        gfx_text_fit(TEXT, MARGIN, y + LH + 4, LIST_W, f->conv[0].title, GFX_INK);
        hit((EpdRect){0, y - 26, W, 2 * LH + 20}, A_CONV, 0);
    }
}

static void draw_briefing(const feed_t *f)
{
    draw_header("Briefing");
    if (!f) {
        draw_no_feed();
        return;
    }
    pw_t pw = pw_begin(MARGIN, TOP + 26, LIST_W, lines_from(TOP + 26));
    if (!f->has_briefing) {
        pw_para(&pw, TEXT, "No digest yet. Ask the server to build today's:", GFX_INK, 0);
    } else {
        char d[48];
        snprintf(d, sizeof(d), "Digest of %s", f->brief_date ? f->brief_date : "?");
        pw_para(&pw, TEXT, d, GFX_FAINT, 0);
        pw_para(&pw, TEXT, f->brief_text, GFX_INK, 0);
        pw_gap(&pw);
        for (int i = 0; i < f->n_highlights; i++) {
            char line[420];
            snprintf(line, sizeof(line), "\xe2\x80\xa2 %s", f->highlights[i]); /* • */
            pw_para(&pw, TEXT, line, GFX_INK, 0);
        }
    }
    clamp_page(pw_pages(&pw));
    draw_pager();
    /* digest_refresh: the briefing's date, or today's when there is none */
    char date[12];
    if (f->has_briefing && f->brief_date) {
        snprintf(date, sizeof(date), "%s", f->brief_date);
    } else {
        pda_fmt_ymd(pda_local_day(now_s(), tz()), date, sizeof(date));
    }
    const pda_pending_t *p = NULL;
    const pda_pending_t *all;
    const int np = pda_app_pending(&all);
    for (int i = 0; i < np; i++) {
        if (all[i].info.type == PDA_ACT_DIGEST_REFRESH && strcmp(all[i].info.ref, date) == 0) {
            p = &all[i];
        }
    }
    const EpdRect b = row_button(MARGIN, 250);
    gfx_button(b, p ? "Refresh requested" : "Refresh digest", false);
    hit(b, A_DIGEST, 0);
}

/* Calendar rows: day headers, events, pending cal_adds. */
typedef struct {
    int32_t day;
    int ev;      /* event index, or -1 */
    int pend;    /* pending index, or -1 */
    bool header;
} cal_row_t;

static int build_cal_rows(const feed_t *f, cal_row_t *rows, int max)
{
    const int64_t now = now_s();
    const int32_t today = pda_local_day(now, tz());
    int n = 0;
    int32_t last_day = INT32_MIN;
    const pda_pending_t *pend;
    const int np = pda_app_pending(&pend);
    /* merge events and pending adds day by day for today .. +7 */
    /* The feed covers today .. +7 days; an event queued further out still shows. */
    for (int32_t day = today; day <= today + 31 && n < max; day++) {
        for (int pass = 0; pass < 2; pass++) {
            const int count = pass == 0 ? f->n_events : np;
            for (int i = 0; i < count && n < max - 1; i++) {
                int32_t d;
                if (pass == 0) {
                    const feed_event_t *e = &f->events[i];
                    if (!event_upcoming(e, now)) {
                        continue;
                    }
                    d = e->all_day ? e->start_day : pda_local_day(e->start, tz());
                } else {
                    if (pend[i].info.type != PDA_ACT_CAL_ADD) {
                        continue;
                    }
                    int64_t t;
                    bool date_only;
                    if (!pda_time_parse(pend[i].info.start, &t, &date_only)) {
                        continue;
                    }
                    d = date_only ? pda_local_day(t, 0) : pda_local_day(t, tz());
                }
                if (d != day) {
                    continue;
                }
                if (day != last_day) {
                    rows[n++] = (cal_row_t){.day = day, .ev = -1, .pend = -1, .header = true};
                    last_day = day;
                }
                rows[n++] = (cal_row_t){.day = day, .ev = pass == 0 ? i : -1, .pend = pass ? i : -1};
            }
        }
    }
    return n;
}

static void draw_calendar(const feed_t *f)
{
    draw_header("Calendar");
    if (!f) {
        draw_no_feed();
        return;
    }
    static cal_row_t rows[FEED_MAX_EVENTS + OUTBOX_MAX + 16];
    const int n = build_cal_rows(f, rows, (int)(sizeof(rows) / sizeof(rows[0])));
    /* Row heights: one line, two when an event has a place. Pages break by height. */
    const int y0 = TOP + 26;
    int page_start[64] = {0}, pages = 1;
    for (int i = 0, h = 0; i < n; i++) {
        const int rh = (rows[i].ev >= 0 && f->events[rows[i].ev].where ? 2 * LH : LH) + 6;
        if (h + rh > LIST_BOTTOM - y0 + 24 && pages < 64) {
            page_start[pages++] = i;
            h = 0;
        }
        h += rh;
    }
    clamp_page(pages);
    const int first = page_start[ui.page], last = ui.page + 1 < pages ? page_start[ui.page + 1] : n;
    const pda_pending_t *pend;
    pda_app_pending(&pend);
    const pda_failed_t *failed;
    const int nf = pda_app_failed(&failed);
    int y = y0;
    if (n == 0) {
        gfx_text(TEXT, MARGIN, y, "No events from today to +7 days.", GFX_FAINT);
    }
    for (int i = first; i < last; i++) {
        const cal_row_t *r = &rows[i];
        if (r->header) {
            char d[24];
            pda_fmt_day(r->day, d, sizeof(d));
            gfx_text(TEXT, MARGIN, y, d, GFX_FAINT);
            gfx_hline(MARGIN + gfx_text_width(TEXT, d) + 10, y - 8, LIST_W - gfx_text_width(TEXT, d) - 10, 0xC0);
            y += LH + 6;
        } else if (r->ev >= 0) {
            const feed_event_t *e = &f->events[r->ev];
            char when[32];
            fmt_event_when(e, when, sizeof(when), false);
            gfx_text(TEXT, MARGIN + 10, y, when, GFX_INK);
            gfx_text_fit(TEXT, MARGIN + 170, y, LIST_W - 170, e->title, GFX_INK);
            const int rh = (e->where ? 2 * LH : LH) + 6;
            if (e->where) {
                gfx_text_fit(TEXT, MARGIN + 170, y + LH, LIST_W - 170, e->where, GFX_FAINT);
            }
            hit((EpdRect){0, y - 26, W, rh}, A_EVENT, r->ev);
            y += rh;
        } else {
            const pda_action_info_t *a = &pend[r->pend].info;
            int64_t t = 0;
            bool date_only = false;
            pda_time_parse(a->start, &t, &date_only);
            char when[16] = "all day", line[160];
            if (!date_only && !a->all_day) {
                pda_fmt_hm(t, tz(), when, sizeof(when));
            }
            gfx_text(TEXT, MARGIN + 10, y, when, GFX_PENDING);
            snprintf(line, sizeof(line), "%s  (pending%s)", a->title,
                     pend[r->pend].listed ? ", sent" : " \xe2\x80\x93 tap to undo");
            gfx_text_fit(TEXT, MARGIN + 170, y, LIST_W - 170, line, GFX_PENDING);
            hit((EpdRect){0, y - 26, W, LH + 6}, A_CANCEL_PENDING, r->pend);
            y += LH + 6;
        }
    }
    for (int i = 0; i < nf && y < LIST_BOTTOM; i++) {
        if (failed[i].type == PDA_ACT_CAL_ADD) {
            char line[120];
            snprintf(line, sizeof(line), "Couldn't sync: %s", failed[i].what);
            gfx_text_fit(TEXT, MARGIN, y, LIST_W, line, GFX_INK);
            y += LH;
        }
    }
    draw_pager();
    const EpdRect plus = row_button(MARGIN, 150);
    gfx_button(plus, "+ New", false);
    hit(plus, A_PLUS, 0);
}

static const pda_failed_t *failed_item(int64_t id)
{
    const pda_failed_t *f;
    const int n = pda_app_failed(&f);
    for (int i = 0; i < n; i++) {
        if (f[i].type == PDA_ACT_ITEM_STATUS && f[i].item_id == id) {
            return &f[i];
        }
    }
    return NULL;
}

static void draw_actions(const feed_t *f)
{
    draw_header("Action items");
    if (!f) {
        draw_no_feed();
        return;
    }
    /* rows of three lines: the item (wrapped to two), then due / owner and "→ cal" */
    const int row_h = 3 * LH + 16;
    int y = TOP + 26;
    gfx_text_fit(TEXT, MARGIN, y, LIST_W, "Tap: done \xc2\xb7 hold: dismiss \xc2\xb7 tap again: undo",
                 GFX_FAINT);
    y += LH + 14;
    const int per_page = (LIST_BOTTOM - y + 24) / row_h;
    clamp_page((f->n_actions + per_page - 1) / per_page);
    if (f->n_actions == 0) {
        gfx_text(TEXT, MARGIN, y, "No open action items.", GFX_INK);
    }
    const int32_t today = pda_local_day(now_s(), tz());
    for (int i = ui.page * per_page; i < f->n_actions && i < (ui.page + 1) * per_page; i++) {
        const feed_action_t *a = &f->actions[i];
        const pda_pending_t *p = pda_app_pending_item(a->id);
        const bool pending_close = p && strcmp(p->info.status, "open") != 0;
        const pda_failed_t *fail = failed_item(a->id);
        const uint8_t color = pending_close ? GFX_PENDING : GFX_INK;
        /* checkbox */
        const EpdRect box = {MARGIN, y - 22, 28, 28};
        gfx_frame(box, color == GFX_INK ? 0x00 : 0x70);
        if (pending_close) {
            gfx_text(TEXT, box.x + 5, y + 1, strcmp(p->info.status, "done") == 0 ? "\xe2\x9c\x93" : "\xc3\x97",
                     GFX_PENDING); /* ✓ or × */
        }
        const int tx = MARGIN + 42, tw = LIST_W - 42;
        const int lines = gfx_wrap(TEXT, tx, y, tw, a->text, color, 0, 2);
        if (pending_close) {
            /* strike each drawn line */
            for (int l = 0; l < (lines < 2 ? lines : 2); l++) {
                gfx_strike(TEXT, tx, y + l * LH, l == 0 && lines > 1 ? tw : tw / 2);
            }
        }
        /* meta line, with "→ cal" at its right end */
        const int my = y + 2 * LH;
        char meta[200], due[40] = "";
        if (a->due_day != FEED_NO_DAY) {
            char d[24];
            pda_fmt_day(a->due_day, d, sizeof(d));
            snprintf(due, sizeof(due), "%s%s", a->due_day < today ? "overdue, " : "due ", d);
        }
        if (fail) {
            snprintf(meta, sizeof(meta), "couldn't sync \xe2\x80\x93 refused");
        } else if (p) {
            snprintf(meta, sizeof(meta), "%s, %s",
                     strcmp(p->info.status, "dismissed") == 0 ? "dismissed" : p->info.status,
                     p->listed ? "sent to the phone" : "waiting (tap to undo)");
        } else {
            snprintf(meta, sizeof(meta), "%s%s%s", due, a->owner && due[0] ? " \xc2\xb7 " : "",
                     a->owner ? a->owner : "");
        }
        const int cal_w = a->due[0] ? 110 : 0;
        gfx_text_fit(TEXT, tx, my, tw - cal_w - 10, meta, fail ? GFX_INK : GFX_FAINT);
        hit((EpdRect){0, y - 30, W, row_h - (cal_w ? 54 : 0)}, A_ITEM, i);
        if (cal_w) {
            hit((EpdRect){0, my - 30, W - MARGIN - cal_w - 10, 54}, A_ITEM, i);
            const EpdRect cal = {W - MARGIN - cal_w, my - 32, cal_w, 46};
            gfx_button(cal, "\xe2\x86\x92 cal", false); /* → cal */
            hit(cal, A_ITEM_CAL, i);
        }
        y += row_h;
        gfx_hline(MARGIN, y - 36, LIST_W, 0xD0);
    }
    draw_pager();
}

static void draw_conversations(const feed_t *f)
{
    draw_header("Conversations");
    if (!f) {
        draw_no_feed();
        return;
    }
    /* rows: the title on up to two lines, then time and people */
    const int row_h = 3 * LH + 22;
    int y = TOP + 30;
    const int per_page = (LIST_BOTTOM - y + 24) / row_h;
    clamp_page((f->n_conv + per_page - 1) / per_page);
    if (f->n_conv == 0) {
        gfx_text(TEXT, MARGIN, y, "No conversations in the last 48 hours.", GFX_FAINT);
    }
    for (int i = ui.page * per_page; i < f->n_conv && i < (ui.page + 1) * per_page; i++) {
        const feed_conv_t *c = &f->conv[i];
        const int lines = gfx_wrap(TEXT, MARGIN, y, LIST_W, c->title, GFX_INK, 0, 2);
        char meta[200] = "", when[48] = "";
        if (c->at) {
            char d[24], t[12];
            pda_fmt_date(c->at, tz(), d, sizeof(d));
            pda_fmt_hm(c->at, tz(), t, sizeof(t));
            snprintf(when, sizeof(when), "%s %s", d, t);
        }
        size_t m = (size_t)snprintf(meta, sizeof(meta), "%s", when);
        for (int k = 0; k < c->n_people && m < sizeof(meta) - 4; k++) {
            m += (size_t)snprintf(meta + m, sizeof(meta) - m, "%s%s", k == 0 ? (m ? " \xc2\xb7 " : "") : ", ",
                                  c->people[k]);
        }
        gfx_text_fit(TEXT, MARGIN + 20, y + (lines > 1 ? 2 : 1) * LH, LIST_W - 20, meta, GFX_FAINT);
        hit((EpdRect){0, y - 30, W, row_h}, A_CONV, i);
        y += row_h;
        gfx_hline(MARGIN, y - 36, LIST_W, 0xD0);
    }
    draw_pager();
}

static void draw_conv_detail(const feed_t *f)
{
    if (!f || ui.sel < 0 || ui.sel >= f->n_conv) {
        go(UI_CONVERSATIONS);
        draw_conversations(f);
        return;
    }
    const feed_conv_t *c = &f->conv[ui.sel];
    draw_header("Conversation");
    pw_t pw = pw_begin(MARGIN, TOP + 26, LIST_W, lines_from(TOP + 26));
    pw_para(&pw, TEXT, c->title, GFX_INK, 0);
    char meta[200] = "";
    size_t m = 0;
    if (c->at) {
        char d[24], t[12];
        pda_fmt_date(c->at, tz(), d, sizeof(d));
        pda_fmt_hm(c->at, tz(), t, sizeof(t));
        m = (size_t)snprintf(meta, sizeof(meta), "%s %s", d, t);
    }
    for (int k = 0; k < c->n_people && m < sizeof(meta) - 4; k++) {
        m += (size_t)snprintf(meta + m, sizeof(meta) - m, "%s%s", k == 0 ? (m ? " \xc2\xb7 " : "") : ", ", c->people[k]);
    }
    pw_para(&pw, TEXT, meta, GFX_FAINT, 0);
    pw_gap(&pw);
    pw_para(&pw, TEXT, c->summary, GFX_INK, 0);
    pw_gap(&pw);
    for (int i = 0; i < c->n_points; i++) {
        char line[420];
        snprintf(line, sizeof(line), "\xe2\x80\xa2 %s", c->points[i]);
        pw_para(&pw, TEXT, line, GFX_INK, 0);
    }
    clamp_page(pw_pages(&pw));
    draw_pager();
    const EpdRect back = row_button(MARGIN, 110);
    const EpdRect tr = row_button(MARGIN + 122, 160);
    gfx_button(back, "Back", false);
    gfx_button(tr, "Transcript", false);
    hit(back, A_BACK, 0);
    hit(tr, A_TRANSCRIPT, 0);
}

static void load_transcript(void)
{
    if (ui.detail) {
        detail_free(ui.detail);
        free(ui.detail);
        ui.detail = NULL;
    }
    ui.detail = pda_app_load_detail(ui.transcript_ref);
}

/* Open a transcript page: from the cache, or ask the phone for it. */
static void open_transcript(const char *ref)
{
    snprintf(ui.transcript_ref, sizeof(ui.transcript_ref), "%s", ref);
    load_transcript();
    if (!ui.detail && !pda_app_pending_fetch(ref)) {
        char json[PDA_ACTION_BUF];
        const size_t n = pda_action_fetch(json, ref);
        if (n && pda_app_queue(json, n) == 0) {
            toast("Transcript requested; it arrives with the next sync");
        } else {
            toast("Outbox full: sync with the phone first");
        }
    }
    go(UI_TRANSCRIPT);
    ui.page = 0;
}

static void draw_transcript(void)
{
    draw_header("Transcript");
    pw_t pw = pw_begin(MARGIN, TOP + 26, LIST_W, lines_from(TOP + 26));
    if (!ui.detail) {
        const bool pend = pda_app_pending_fetch(ui.transcript_ref) != NULL;
        pw_para(&pw, TEXT, pend ? "Waiting for the phone: the transcript comes with the next sync."
                                : "Not cached on the T5.",
                GFX_INK, 0);
        pw_para(&pw, TEXT, ui.transcript_ref, GFX_FAINT, 0);
    } else {
        const detail_t *d = ui.detail;
        if (d->title) {
            pw_para(&pw, TEXT, d->title, GFX_FAINT, 0);
        }
        static char line[600];
        for (int i = 0; i < d->n_seg; i++) {
            const detail_seg_t *s = &d->seg[i];
            const int sec = (int)s->o;
            snprintf(line, sizeof(line), "%02d:%02d  %s: %s", sec / 60, sec % 60,
                     s->me ? "Me" : (s->s ? s->s : "?"), s->x);
            pw_para(&pw, TEXT, line, s->me ? GFX_ME : GFX_INK, 0);
        }
        if (d->n_seg == 0) {
            pw_para(&pw, TEXT, "No speech in this part.", GFX_FAINT, 0);
        }
    }
    clamp_page(pw_pages(&pw));
    draw_pager();
    const EpdRect back = row_button(MARGIN, 110);
    gfx_button(back, "Back", false);
    hit(back, A_BACK, 0);
    if (ui.detail && ui.detail->next && ui.page == ui.pages - 1) {
        const EpdRect nx = row_button(MARGIN + 122, 160);
        gfx_button(nx, "Next part", false);
        hit(nx, A_NEXT_PAGE, 0);
    }
}

static void draw_event(const feed_t *f)
{
    if (!f || ui.sel < 0 || ui.sel >= f->n_events) {
        go(UI_CALENDAR);
        draw_calendar(f);
        return;
    }
    const feed_event_t *e = &f->events[ui.sel];
    draw_header("Event");
    int y = TOP + 50;
    y += 50 * gfx_wrap(BIG, MARGIN, y, W - 2 * MARGIN, e->title, GFX_INK, 0, 2);
    char when[64];
    fmt_event_when(e, when, sizeof(when), true);
    gfx_text(TEXT, MARGIN, y + 10, when, GFX_INK);
    if (e->where) {
        gfx_text_fit(TEXT, MARGIN, y + 10 + LH + 6, W - 2 * MARGIN, e->where, GFX_FAINT);
    }
    const EpdRect back = row_button(MARGIN, 110);
    gfx_button(back, "Back", false);
    hit(back, A_BACK, 0);
}

static void draw_templates(const feed_t *f)
{
    draw_header("New event");
    gfx_text(TEXT, MARGIN, TOP + 26, "Pick a title:", GFX_FAINT);
    const int n = f && f->n_templates ? f->n_templates : (int)(sizeof(DEFAULT_TEMPLATES) / sizeof(DEFAULT_TEMPLATES[0]));
    const int cols = 2, bw = (W - 2 * MARGIN - (cols - 1) * 12) / cols, bh = 80;
    for (int i = 0; i < n && i < 12; i++) {
        const EpdRect r = {MARGIN + (i % cols) * (bw + 12), TOP + 50 + (i / cols) * (bh + 12), bw, bh};
        gfx_button(r, f && f->n_templates ? f->templates[i] : DEFAULT_TEMPLATES[i], false);
        hit(r, A_TEMPLATE, i);
    }
    const EpdRect back = row_button(MARGIN, 130);
    gfx_button(back, "Cancel", false);
    hit(back, A_BACK, 0);
}

static void start_new_event(const char *title, int32_t day, int64_t from_item)
{
    snprintf(ui.ev_title, sizeof(ui.ev_title), "%s", title);
    const int64_t now = now_s();
    const int32_t today = pda_local_day(now, tz());
    ui.ev_day = day == FEED_NO_DAY || day < today ? today : day;
    /* the next full hour, from 08:00 */
    const int local_min = (int)(((now + tz() * 60) % 86400) / 60);
    ui.ev_min = ui.ev_day == today ? (local_min / 60 + 1) * 60 : 9 * 60;
    if (ui.ev_min < 8 * 60) {
        ui.ev_min = 8 * 60;
    }
    if (ui.ev_min > 23 * 60) {
        ui.ev_min = 23 * 60;
    }
    ui.ev_dur = 30;
    ui.ev_from_item = from_item;
    go(UI_NEW_EVENT);
}

static void draw_new_event(void)
{
    draw_header("New event");
    int y = TOP + 40;
    y += 50 * gfx_wrap(BIG, MARGIN, y, W - 2 * MARGIN, ui.ev_title, GFX_INK, 0, 2);
    /* date: ◀ Sat 31 Oct ▶ */
    char d[24];
    pda_fmt_day(ui.ev_day, d, sizeof(d));
    const EpdRect prev = {MARGIN, y, 76, 56}, next = {W - MARGIN - 76, y, 76, 56};
    gfx_button(prev, "\xe2\x97\x80", false); /* ◀ */
    gfx_button(next, "\xe2\x96\xb6", false); /* ▶ */
    gfx_text(BIG, (W - gfx_text_width(BIG, d)) / 2, y + 42, d, GFX_INK);
    hit(prev, A_DAY, -1);
    hit(next, A_DAY, 1);
    y += 72;
    /* time: −15  09:30  +15 */
    char t[16];
    if (ui.ev_dur == 0) {
        snprintf(t, sizeof(t), "all day");
    } else {
        snprintf(t, sizeof(t), "%02d:%02d", ui.ev_min / 60, ui.ev_min % 60);
    }
    const EpdRect minus = {MARGIN, y, 120, 56}, plus = {W - MARGIN - 120, y, 120, 56};
    gfx_button(minus, "\xe2\x88\x92" "15 min", false);
    gfx_button(plus, "+15 min", false);
    gfx_text(BIG, (W - gfx_text_width(BIG, t)) / 2, y + 42, t, GFX_INK);
    hit(minus, A_STEP, -15);
    hit(plus, A_STEP, 15);
    y += 76;
    /* slots 08:00 .. 19:00, three per row */
    const int cols = 3, gap = 12, sw = (W - 2 * MARGIN - (cols - 1) * gap) / cols, sh = 56;
    for (int i = 0; i < 12; i++) {
        const int min = (8 + i) * 60;
        char s[8];
        snprintf(s, sizeof(s), "%02d:00", 8 + i);
        const EpdRect r = {MARGIN + (i % cols) * (sw + gap), y + (i / cols) * (sh + 10), sw, sh};
        gfx_button(r, s, ui.ev_dur != 0 && ui.ev_min == min);
        hit(r, A_SLOT, min);
    }
    y += 4 * (sh + 10) + 10;
    /* duration */
    static const int durs[] = {15, 30, 60, 0};
    static const char *const dl[] = {"15 min", "30 min", "60 min", "all day"};
    const int dw = (W - 2 * MARGIN - 3 * 10) / 4;
    for (int i = 0; i < 4; i++) {
        const EpdRect r = {MARGIN + i * (dw + 10), y, dw, sh};
        gfx_button(r, dl[i], ui.ev_dur == durs[i]);
        hit(r, A_DURATION, durs[i]);
    }
    const EpdRect cancel = row_button(MARGIN, 150), add = row_button(W - MARGIN - 200, 200);
    gfx_button(cancel, "Cancel", false);
    gfx_button(add, "Add", true);
    hit(cancel, A_BACK, 0);
    hit(add, A_ADD, 0);
}

static void draw_status(const feed_t *f)
{
    draw_header("Status");
    int y = TOP + 26;
    char line[200];
    const int64_t link = pda_app_last_link_s();
    if (ble_pda_is_connected()) {
        snprintf(line, sizeof(line), "Phone: connected");
    } else if (link > 0 && pda_app_time_valid()) {
        char age[24];
        pda_fmt_age(pda_app_now() - link, age, sizeof(age));
        snprintf(line, sizeof(line), "Phone: last seen %s", age);
    } else {
        snprintf(line, sizeof(line), "Phone: not seen since boot");
    }
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    snprintf(line, sizeof(line), "Paired phones: %d", ble_pda_bond_count());
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    snprintf(line, sizeof(line), "Outbox: %d waiting", pda_app_outbox_count());
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    if (f) {
        char d[24] = "?", t[12] = "";
        if (f->generated) {
            pda_fmt_date(f->generated, tz(), d, sizeof(d));
            pda_fmt_hm(f->generated, tz(), t, sizeof(t));
        }
        snprintf(line, sizeof(line), "Feed: version %" PRIu32, f->version);
        gfx_text_fit(TEXT, MARGIN, y, LIST_W, line, GFX_INK);
        y += LH;
        snprintf(line, sizeof(line), "   built %s %s%s%s%s", d, t, f->tz ? " (" : "", f->tz ? f->tz : "",
                 f->tz ? ")" : "");
    } else {
        snprintf(line, sizeof(line), "Feed: none");
    }
    gfx_text_fit(TEXT, MARGIN, y, LIST_W, line, GFX_INK);
    y += LH;
    const int z = tz() < 0 ? -tz() : tz();
    snprintf(line, sizeof(line), "Clock: %s, UTC%c%02d:%02d", pda_app_time_valid() ? "set" : "not set",
             tz() < 0 ? '-' : '+', z / 60, z % 60);
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    t5_battery_t b;
    if (t5_battery_read(&b) == ESP_OK && b.present) {
        snprintf(line, sizeof(line), "Battery: %d%%, %d mV%s%s", b.percent, b.millivolts,
                 b.charging ? ", charging" : "", b.vbus ? ", USB" : "");
    } else {
        snprintf(line, sizeof(line), "Battery: unknown");
    }
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    snprintf(line, sizeof(line), "Screen: portrait %d\xc2\xb0", pda_epd_rotation());
    gfx_text(TEXT, MARGIN, y, line, GFX_INK);
    y += LH;
    snprintf(line, sizeof(line), "Firmware %s, board t5_epaper_pro", PDA_FW_VERSION);
    gfx_text_fit(TEXT, MARGIN, y, LIST_W, line, GFX_FAINT);
    y += LH;
    gfx_text(TEXT, MARGIN, y, "BLE name odysseus-pda", GFX_FAINT);

    /* buttons, two per row above the bottom row, wipe in the bottom row */
    const int bw = (W - 2 * MARGIN - 12) / 2, bh = ROW_H - 4;
    const EpdRect pair = {MARGIN, ROW_Y - 2 * (bh + 12), bw, bh};
    const EpdRect clean = {MARGIN + bw + 12, ROW_Y - 2 * (bh + 12), bw, bh};
    const EpdRect rot = {MARGIN, ROW_Y - (bh + 12), bw, bh};
    const EpdRect wipe = {MARGIN, ROW_Y, bw, bh};
    char pl[48];
    const int left = ble_pda_window_left_s();
    if (left > 0) {
        snprintf(pl, sizeof(pl), "Pairing open (%d s)", left);
    } else {
        snprintf(pl, sizeof(pl), "Pair a phone");
    }
    gfx_button(pair, pl, left > 0);
    gfx_button(clean, "Clean refresh", false);
    gfx_button(wipe, "Wipe cache (hold)", false);
    gfx_button(rot, "Turn 180\xc2\xb0", false);
    hit(rot, A_ROTATE, 0);
    hit(pair, A_PAIR, 0);
    hit(clean, A_CLEAN, 0);
    hit(wipe, A_WIPE, 0);
}

static void draw_passkey(void)
{
    if (ui.passkey < 0) {
        return;
    }
    const EpdRect r = {W / 2 - 260, H / 2 - 130, 520, 260};
    gfx_fill(r, 0xFF);
    gfx_frame(r, 0x00);
    gfx_frame((EpdRect){r.x + 4, r.y + 4, r.width - 8, r.height - 8}, 0x00);
    gfx_text(TEXT, r.x + 30, r.y + 50, "Pairing code: type it on the phone", GFX_INK);
    char code[16];
    snprintf(code, sizeof(code), "%03" PRId32 " %03" PRId32, ui.passkey / 1000, ui.passkey % 1000);
    /* the big font, drawn twice offset by a pixel for weight */
    const int cw = gfx_text_width(BIG, code);
    gfx_text(BIG, r.x + (r.width - cw) / 2, r.y + 150, code, GFX_INK);
    gfx_text(BIG, r.x + (r.width - cw) / 2 + 1, r.y + 150, code, GFX_INK);
    gfx_text(TEXT, r.x + 30, r.y + 220, "Only shown while the pairing window is open.", GFX_FAINT);
}

static void draw(void)
{
    uint8_t *fb = pda_epd_fb();
    gfx_begin(fb);
    pda_epd_clear_fb();
    ui.n_hits = 0;
    pda_app_lock();
    const feed_t *f = pda_app_feed();
    switch (ui.screen) {
    case UI_TODAY: draw_today(f); break;
    case UI_BRIEFING: draw_briefing(f); break;
    case UI_CALENDAR: draw_calendar(f); break;
    case UI_ACTIONS: draw_actions(f); break;
    case UI_CONVERSATIONS: draw_conversations(f); break;
    case UI_STATUS: draw_status(f); break;
    case UI_CONV_DETAIL: draw_conv_detail(f); break;
    case UI_TRANSCRIPT: draw_transcript(); break;
    case UI_EVENT: draw_event(f); break;
    case UI_TEMPLATES: draw_templates(f); break;
    case UI_NEW_EVENT: draw_new_event(); break;
    default: break;
    }
    pda_app_unlock();
    draw_tabs();
    draw_toast();
    draw_passkey();
}

/* ---- input ---- */

static void queue_item_status(const feed_action_t *a, const char *status)
{
    char json[PDA_ACTION_BUF];
    const size_t n = pda_action_item_status(json, a->id, status);
    const int rc = n ? pda_app_queue(json, n) : -2;
    if (rc == -1) {
        toast("Outbox full: sync with the phone first");
    } else if (rc != 0) {
        toast("Couldn't save the action");
    }
}

static void on_item(int idx, bool is_long)
{
    pda_app_lock();
    const feed_t *f = pda_app_feed();
    if (!f || idx < 0 || idx >= f->n_actions) {
        pda_app_unlock();
        return;
    }
    const feed_action_t a = f->actions[idx]; /* strings stay valid: the feed is only swapped in the UI's lock */
    const pda_pending_t *p = pda_app_pending_item(a.id);
    const uint32_t pid = p ? p->id : 0;
    const bool listed = p && p->listed;
    pda_app_unlock();
    if (p) {
        if (listed) {
            /* Already with the phone: the reverse action puts it back. */
            queue_item_status(&a, "open");
            toast("Reopening: sent to the phone at the next sync");
        } else if (pda_app_cancel(pid) == 0) {
            toast("Undone");
        }
        return;
    }
    queue_item_status(&a, is_long ? "dismissed" : "done");
}

static void act(const hit_t *h, bool is_long)
{
    const feed_t *f;
    switch ((action_t)h->act) {
    case A_TAB:
    case A_OPEN:
        go((ui_screen_t)h->arg);
        break;
    case A_PAGE:
        if (ui.page + h->arg >= 0 && ui.page + h->arg < ui.pages) {
            ui.page += h->arg;
        }
        break;
    case A_ITEM:
        on_item(h->arg, is_long);
        break;
    case A_ITEM_CAL:
        pda_app_lock();
        f = pda_app_feed();
        if (f && h->arg < f->n_actions) {
            const feed_action_t *a = &f->actions[h->arg];
            start_new_event(a->text, a->due_day, a->id);
        }
        pda_app_unlock();
        break;
    case A_CONV:
        ui.sel = h->arg;
        go(UI_CONV_DETAIL);
        break;
    case A_EVENT:
        ui.sel = h->arg;
        go(UI_EVENT);
        break;
    case A_BACK:
        go(ui.screen == UI_TRANSCRIPT ? UI_CONV_DETAIL : ui.back_to);
        break;
    case A_DIGEST: {
        char date[12];
        pda_app_lock();
        f = pda_app_feed();
        if (f && f->has_briefing && f->brief_date) {
            snprintf(date, sizeof(date), "%s", f->brief_date);
        } else {
            pda_fmt_ymd(pda_local_day(now_s(), tz()), date, sizeof(date));
        }
        pda_app_unlock();
        char json[PDA_ACTION_BUF];
        const size_t n = pda_action_digest_refresh(json, date);
        if (n && pda_app_queue(json, n) == 0) {
            toast("Digest refresh requested");
        }
        break;
    }
    case A_TRANSCRIPT: {
        char ref[PDA_REF_MAX] = "";
        pda_app_lock();
        f = pda_app_feed();
        if (f && ui.sel >= 0 && ui.sel < f->n_conv) {
            snprintf(ref, sizeof(ref), "transcript:%s@0-%d", f->conv[ui.sel].id, TRANSCRIPT_PAGE_S);
        }
        pda_app_unlock();
        if (ref[0]) {
            open_transcript(ref);
        }
        break;
    }
    case A_NEXT_PAGE:
        if (ui.detail && ui.detail->next) {
            char ref[PDA_REF_MAX];
            snprintf(ref, sizeof(ref), "%s", ui.detail->next);
            open_transcript(ref);
            ui.screen_changed = true;
        }
        break;
    case A_PLUS:
        go(UI_TEMPLATES);
        break;
    case A_TEMPLATE: {
        pda_app_lock();
        f = pda_app_feed();
        const char *t = f && f->n_templates && h->arg < f->n_templates ? f->templates[h->arg]
                        : h->arg < 3                                     ? DEFAULT_TEMPLATES[h->arg]
                                                                         : "Event";
        char title[96];
        snprintf(title, sizeof(title), "%s", t);
        pda_app_unlock();
        start_new_event(title, FEED_NO_DAY, -1);
        break;
    }
    case A_DAY: {
        const int32_t today = pda_local_day(now_s(), tz());
        const int32_t d = ui.ev_day + h->arg;
        if (d >= today && d <= today + 30) {
            ui.ev_day = d;
        }
        break;
    }
    case A_SLOT:
        ui.ev_min = h->arg;
        if (ui.ev_dur == 0) {
            ui.ev_dur = 30;
        }
        break;
    case A_STEP:
        ui.ev_min += h->arg;
        if (ui.ev_min < 0) {
            ui.ev_min = 0;
        }
        if (ui.ev_min > 24 * 60 - 15) {
            ui.ev_min = 24 * 60 - 15;
        }
        break;
    case A_DURATION:
        ui.ev_dur = h->arg;
        break;
    case A_ADD: {
        char json[PDA_ACTION_BUF], start[32], end[32];
        size_t n;
        if (ui.ev_dur == 0) {
            pda_fmt_ymd(ui.ev_day, start, sizeof(start));
            n = pda_action_cal_add(json, ui.ev_title, start, NULL, true, ui.ev_from_item);
        } else {
            const int64_t t0 = (int64_t)ui.ev_day * 86400 + (int64_t)ui.ev_min * 60 - tz() * 60;
            pda_fmt_iso_local(t0, tz(), start, sizeof(start));
            pda_fmt_iso_local(t0 + (int64_t)ui.ev_dur * 60, tz(), end, sizeof(end));
            n = pda_action_cal_add(json, ui.ev_title, start, end, false, ui.ev_from_item);
        }
        const int rc = n ? pda_app_queue(json, n) : -2;
        toast(rc == 0 ? "Event queued for the phone" : rc == -1 ? "Outbox full: sync first" : "Couldn't save the event");
        go(UI_CALENDAR);
        break;
    }
    case A_CANCEL_PENDING: {
        const pda_pending_t *pend;
        pda_app_lock();
        const int np = pda_app_pending(&pend);
        const uint32_t id = h->arg < np ? pend[h->arg].id : 0;
        const bool listed = h->arg < np && pend[h->arg].listed;
        pda_app_unlock();
        if (listed) {
            toast("Already sent to the phone");
        } else if (id && pda_app_cancel(id) == 0) {
            toast("Undone");
        }
        break;
    }
    case A_PAIR:
        ble_pda_open_window();
        toast("Pairing open for 2 minutes: pair from the phone or PC now");
        break;
    case A_CLEAN:
        pda_epd_update(PDA_EPD_CLEAN);
        break;
    case A_ROTATE:
        pda_epd_set_rotation(pda_epd_rotation() == 90 ? 270 : 90);
        ui.screen_changed = true; /* everything moves: redraw as a new screen */
        break;
    case A_WIPE:
        if (is_long) {
            pda_app_wipe();
            toast("Feed, details and outbox wiped");
        } else {
            toast("Hold the button to wipe the cache");
        }
        break;
    default:
        break;
    }
}

static void on_gesture(gesture_kind_t k, int x, int y)
{
    pda_app_touch_activity();
    if (ui.passkey >= 0) {
        return; /* the code stays up until pairing ends */
    }
    if (k == GESTURE_SWIPE_UP || k == GESTURE_SWIPE_DOWN) {
        const int d = k == GESTURE_SWIPE_UP ? 1 : -1;
        if (ui.page + d >= 0 && ui.page + d < ui.pages) {
            ui.page += d;
        }
        return;
    }
    if (k == GESTURE_SWIPE_LEFT || k == GESTURE_SWIPE_RIGHT) {
        int s = ui.screen < UI_CONV_DETAIL ? (int)ui.screen : (int)UI_TODAY;
        s = (s + (k == GESTURE_SWIPE_LEFT ? 1 : 5)) % 6;
        go((ui_screen_t)s);
        return;
    }
    /* last matching region wins: buttons drawn over rows */
    for (int i = ui.n_hits - 1; i >= 0; i--) {
        const EpdRect *r = &ui.hits[i].r;
        if (x >= r->x && x < r->x + r->width && y >= r->y && y < r->y + r->height) {
            act(&ui.hits[i], k == GESTURE_LONG);
            return;
        }
    }
}

static void on_detail_arrived(uint32_t ref_crc)
{
    if (ui.screen == UI_TRANSCRIPT &&
        crc32_update(0, ui.transcript_ref, strlen(ui.transcript_ref)) == ref_crc) {
        load_transcript();
    }
}

/* Redraw and push to the panel with the refresh policy. */
static void refresh(pda_epd_kind_t kind)
{
    draw();
    if (ui.screen_changed && kind == PDA_EPD_PARTIAL) {
        kind = PDA_EPD_FULL;
    }
    ui.screen_changed = false;
    pda_epd_update(kind);
}

static void ui_task(void *arg)
{
    (void)arg;
    refresh(PDA_EPD_FULL);
    for (;;) {
        pda_ev_t ev;
        if (!pda_app_wait_event(&ev, 3600 * 1000)) {
            continue;
        }
        switch (ev.type) {
        case PDA_EV_GESTURE:
            on_gesture((gesture_kind_t)ev.gesture, ev.x, ev.y);
            refresh(PDA_EPD_PARTIAL);
            break;
        case PDA_EV_BUTTON: {
            const t5_button_t b = (t5_button_t)(ev.arg >> 1);
            const bool lng = ev.arg & 1;
            pda_app_touch_activity();
            if (b == T5_BUTTON_BOOT && lng) {
                ble_pda_open_window();
                go(UI_STATUS);
                toast("Pairing open for 2 minutes");
                refresh(PDA_EPD_FULL);
            } else if (b == T5_BUTTON_BOOT) {
                go(ui.screen >= UI_CONV_DETAIL ? ui.back_to : UI_TODAY);
                refresh(PDA_EPD_PARTIAL);
            } else {
                refresh(PDA_EPD_CLEAN);
            }
            break;
        }
        case PDA_EV_FEED:
            refresh(PDA_EPD_FULL);
            break;
        case PDA_EV_DETAIL:
            on_detail_arrived((uint32_t)ev.arg);
            refresh(PDA_EPD_PARTIAL);
            break;
        case PDA_EV_OUTBOX:
            if (ev.arg == 1) {
                toast("The server refused an action: couldn't sync");
            }
            refresh(PDA_EPD_PARTIAL);
            break;
        case PDA_EV_PASSKEY:
            ui.passkey = ev.arg;
            refresh(PDA_EPD_PARTIAL);
            break;
        case PDA_EV_LINK:
            refresh(PDA_EPD_PARTIAL);
            break;
        case PDA_EV_TICK: {
            /* The minute clock: only the header changes; it doesn't count as a partial. */
            draw();
            pda_epd_update_light(header_area());
            break;
        }
        case PDA_EV_REDRAW:
            ui.screen_changed = true;
            refresh(ev.arg ? PDA_EPD_CLEAN : PDA_EPD_FULL);
            break;
        case PDA_EV_SCREEN:
            if (ev.arg >= 0 && ev.arg < UI_SCREEN_COUNT) {
                if (ev.arg == UI_CONV_DETAIL || ev.arg == UI_EVENT) {
                    ui.sel = 0;
                }
                go((ui_screen_t)ev.arg);
                refresh(PDA_EPD_PARTIAL);
            }
            break;
        default:
            break;
        }
    }
}

esp_err_t ui_pda_start(void)
{
    ui.screen = UI_TODAY;
    ui.back_to = UI_TODAY;
    ESP_LOGI(TAG, "starting");
    return xTaskCreate(ui_task, "ui", TASK_STACK, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
