/* The Today tab (§5) rendered on the host: ui_today.c and gfx.c draw into an 8-bit image
 * through a stand-in for epdiy (host/epdiy.h). Checks the layout (everything inside the
 * content area, hit regions, pages, the tick shown at once) and writes the pages as PNG
 * for a look:
 *
 *   cmake -S firmware/pda/test -B build-pda-host && cmake --build build-pda-host &&
 *   ctest --test-dir build-pda-host -R test_ui_today   # -> build-pda-host/today_screens/
 *
 * The header and the tab bar around the content are a stand-in of ui_pda.c's (that file
 * needs ESP-IDF); ui_today.c, gfx.c, the fonts and the plan are the firmware's own. */
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <zlib.h>

#include "check.h"
#include "feed.h"
#include "gfx.h"
#include "pda_time.h"
#include "today_plan.h"
#include "ui_layout.h"
#include "ui_today.h"

#define NOW 1792479600 /* 2026-10-20 07:00 UTC, 09:00 in Berlin */
#define TZ 120
#define TODAY 20746

static uint8_t fb[HOST_FB_W * HOST_FB_H];
static feed_t feed;
static today_plan_t plan;

typedef struct {
    EpdRect r;
    ui_today_hit_t kind;
    int32_t arg;
} hit_rec_t;
static hit_rec_t hits[64];
static int n_hits;
static int ticked_note = -1, ticked_item = -1; /* a queued todo_toggle to done */

static char *load(const char *name, size_t *len)
{
    const char *dir = getenv("FIXTURES");
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", dir ? dir : "fixtures", name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "missing fixture %s\n", path);
        exit(1);
    }
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(*len + 1);
    if (fread(buf, 1, *len, f) != *len) {
        exit(1);
    }
    buf[*len] = 0;
    fclose(f);
    return buf;
}

static void on_hit(void *ctx, EpdRect r, ui_today_hit_t kind, int32_t arg)
{
    (void)ctx;
    if (n_hits < 64) {
        hits[n_hits++] = (hit_rec_t){r, kind, arg};
    }
}

static void todo_state(void *ctx, int note, int item, ui_today_todo_t *out)
{
    (void)ctx;
    if (note == ticked_note && item == ticked_item) {
        out->done = true;
    }
}

/* ---- PNG (8-bit grey) ---- */

static void put32(FILE *f, uint32_t v)
{
    const uint8_t b[4] = {(uint8_t)(v >> 24), (uint8_t)(v >> 16), (uint8_t)(v >> 8), (uint8_t)v};
    fwrite(b, 1, 4, f);
}

static void chunk(FILE *f, const char *type, const uint8_t *data, uint32_t len)
{
    put32(f, len);
    fwrite(type, 1, 4, f);
    fwrite(data, 1, len, f);
    uLong crc = crc32(0, (const Bytef *)type, 4);
    crc = crc32(crc, data, len);
    put32(f, (uint32_t)crc);
}

static void write_png(const char *name)
{
    const char *dir = getenv("OUT_DIR");
    if (!dir) {
        dir = "today_screens";
    }
    mkdir(dir, 0755);
    char path[600];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    const size_t raw_len = (size_t)HOST_FB_H * (HOST_FB_W + 1);
    uint8_t *raw = malloc(raw_len);
    for (int y = 0; y < HOST_FB_H; y++) {
        raw[(size_t)y * (HOST_FB_W + 1)] = 0; /* filter: none */
        memcpy(raw + (size_t)y * (HOST_FB_W + 1) + 1, fb + (size_t)y * HOST_FB_W, HOST_FB_W);
    }
    uLongf zlen = compressBound(raw_len);
    uint8_t *z = malloc(zlen);
    CHECK(compress(z, &zlen, raw, raw_len) == Z_OK);
    FILE *f = fopen(path, "wb");
    CHECK(f != NULL);
    if (f) {
        static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
        fwrite(sig, 1, 8, f);
        const uint8_t ihdr[13] = {0, 0, HOST_FB_W >> 8, HOST_FB_W & 0xFF, 0, 0, HOST_FB_H >> 8, HOST_FB_H & 0xFF,
                                  8, 0, 0, 0, 0};
        chunk(f, "IHDR", ihdr, 13);
        chunk(f, "IDAT", z, (uint32_t)zlen);
        chunk(f, "IEND", NULL, 0);
        fclose(f);
        printf("wrote %s\n", path);
    }
    free(z);
    free(raw);
}

/* ---- the frame around the content: a stand-in of ui_pda.c's header, pager and tabs ---- */

static void frame(int page, int pages)
{
    gfx_text(BIG, MARGIN, 48, "Today", GFX_INK);
    gfx_text_right(BIG, W - MARGIN, 48, "09:00", GFX_INK);
    gfx_text_fit(TEXT, MARGIN, 86, LIST_W, "Tue 20 Oct \xc2\xb7 87% \xc2\xb7 synced 2 min ago", GFX_INK);
    gfx_hline(0, HEADER_H - 2, W, 0x00);
    gfx_hline(0, HEADER_H - 1, W, 0x00);
    if (pages > 1) {
        const EpdRect down = {W - MARGIN - PAGER_BTN, ROW_Y, PAGER_BTN, ROW_H - 4};
        const EpdRect up = {down.x - 70 - PAGER_BTN, ROW_Y, PAGER_BTN, ROW_H - 4};
        gfx_button(up, "\xe2\x96\xb2", false);
        gfx_button(down, "\xe2\x96\xbc", false);
        char pg[32];
        snprintf(pg, sizeof(pg), "%d/%d", page + 1, pages);
        gfx_text(TEXT, up.x + PAGER_BTN + (70 - gfx_text_width(TEXT, pg)) / 2, ROW_Y + 33, pg, GFX_INK);
    }
    static const char *const tabs[6] = {"Today", "Brief", "Cal", "Tasks", "Talks", "Status"};
    const int tw = W / 6;
    gfx_hline(0, TAB_Y, W, 0x00);
    for (int i = 0; i < 6; i++) {
        gfx_button((EpdRect){i * tw + 4, TAB_Y + 6, tw - 8, TAB_H - 10}, tabs[i], i == 0);
    }
}

/* Draws one page of the Today content; checks that ink and hit regions stay in the content
 * area (between the header and the tab bar) and returns the page count. */
static int render(int *page)
{
    memset(fb, 0xFF, sizeof(fb));
    gfx_begin(fb);
    n_hits = 0;
    host_fb_outside = 0;
    const ui_today_in_t in = {.f = &feed, .plan = &plan, .todo_state = todo_state, .hit = on_hit};
    const int pages = ui_today_draw(&in, page);
    CHECK_EQ(host_fb_outside, 0);
    int ink_out = 0;
    for (int y = 0; y < HOST_FB_H; y++) {
        for (int x = 0; x < HOST_FB_W; x++) {
            const bool in_area = y >= HEADER_H && y < BOTTOM && x >= 4 && x < W - 4;
            ink_out += fb[y * HOST_FB_W + x] != 0xFF && !in_area;
        }
    }
    CHECK_EQ(ink_out, 0);
    for (int i = 0; i < n_hits; i++) {
        const EpdRect r = hits[i].r;
        CHECK(r.x >= 0 && r.y >= TOP && r.x + r.width <= W && r.y + r.height <= BOTTOM);
        if (hits[i].kind == UI_TODAY_HIT_TODO) {
            const int note = hits[i].arg / 64, item = hits[i].arg % 64;
            CHECK(note < feed.n_todos && item < feed.todos[note].n_items);
        } else if (hits[i].kind == UI_TODAY_HIT_EVENT) {
            CHECK(hits[i].arg >= 0 && hits[i].arg < feed.n_events);
        }
    }
    frame(*page, pages);
    return pages;
}

static int count_hits(ui_today_hit_t kind)
{
    int n = 0;
    for (int i = 0; i < n_hits; i++) {
        n += hits[i].kind == kind;
    }
    return n;
}

static const hit_rec_t *hit_at(int x, int y)
{
    for (int i = n_hits - 1; i >= 0; i--) {
        const EpdRect r = hits[i].r;
        if (x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height) {
            return &hits[i];
        }
    }
    return NULL;
}

/* Every page: each plan item gets exactly one tap region over all pages. */
static int render_all(const char *name)
{
    int page = 0, items = 0;
    const int pages = render(&page);
    for (int p = 0; p < pages; p++) {
        page = p;
        render(&page);
        CHECK_EQ(page, p);
        items += count_hits(UI_TODAY_HIT_TODO) + count_hits(UI_TODAY_HIT_EVENT);
        CHECK_EQ(count_hits(UI_TODAY_HIT_BRIEF), feed.has_briefing ? 1 : 0);
        char file[96];
        snprintf(file, sizeof(file), "%s_p%d.png", name, p + 1);
        write_png(file);
    }
    int want = 0;
    for (int s = 0; s < TODAY_SECTIONS; s++) {
        want += plan.sec[s].n;
    }
    CHECK_EQ(items, want);
    return pages;
}

static void load_feed(const char *name)
{
    size_t len;
    char *js = load(name, &len);
    CHECK_EQ(feed_parse(js, len, &feed), 0);
    free(js);
}

static int note_idx(const char *id)
{
    for (int n = 0; n < feed.n_todos; n++) {
        if (strcmp(feed.todos[n].id, id) == 0) {
            return n;
        }
    }
    return -1;
}

/* Ink in a row band: is anything drawn between baselines y0 and y1? */
static int ink(int x0, int y0, int x1, int y1)
{
    int n = 0;
    for (int y = y0; y < y1; y++) {
        for (int x = x0; x < x1; x++) {
            n += fb[y * HOST_FB_W + x] < 0x80;
        }
    }
    return n;
}

static void test_insight(void)
{
    load_feed("feed_insight_demo.json");
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(plan.from_insight);
    CHECK_EQ(render_all("today_insight"), 2);

    /* page 1: the first DO FIRST item is the Work note's item 0; tapping it toggles it */
    int page = 0;
    render(&page);
    const int work = note_idx("note_demo_work");
    int top = -1;
    for (int i = 0; i < n_hits; i++) {
        if (hits[i].kind == UI_TODAY_HIT_TODO && top < 0) {
            top = hits[i].r.y;
            CHECK_EQ(hits[i].arg, work * 64 + 0);
        }
    }
    CHECK(top > TOP);
    const hit_rec_t *h = hit_at(W / 2, top + 20);
    CHECK(h && h->kind == UI_TODAY_HIT_TODO && h->arg == work * 64);
    /* the digest line opens Brief */
    h = hit_at(MARGIN + 20, ROW_Y + 20);
    CHECK(h && h->kind == UI_TODAY_HIT_BRIEF);
    /* nothing to tap in the tab bar */
    CHECK(hit_at(W / 2, TAB_Y + 20) == NULL);

    /* ticked on the T5 (a queued todo_toggle): the box shows ✓ and the text is struck at
     * once, on the same layout */
    const int box_ink = ink(MARGIN + 6, top + 6, MARGIN + 30, top + 30);
    ticked_note = work;
    ticked_item = 0;
    page = 0;
    render(&page);
    CHECK(ink(MARGIN + 6, top + 6, MARGIN + 30, top + 30) > box_ink);
    write_png("today_insight_ticked_p1.png");
    ticked_note = ticked_item = -1;

    /* a page past the end is clamped */
    page = 7;
    CHECK_EQ(render(&page), 2);
    CHECK_EQ(page, 1);

    /* the same feed without its insight: the rules, everything in the content area */
    feed.has_insight = false;
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(!plan.from_insight);
    render_all("today_rules_busy");
    /* DO FIRST shows 5 of 7: "+2 more" leads to the calendar (both left out are events) */
    page = 0;
    render(&page);
    CHECK_EQ(count_hits(UI_TODAY_HIT_CALENDAR), 1);
    CHECK_EQ(count_hits(UI_TODAY_HIT_TASKS), 0);
    feed_free(&feed);
}

static void test_no_insight(void)
{
    load_feed("feed_demo.json");
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(!plan.from_insight);
    CHECK_EQ(render_all("today_rules"), 1);
    feed_free(&feed);

    /* nothing at all, no briefing: one line, no digest line, one page */
    const char *empty = "{\"v\":1,\"version\":1}";
    CHECK_EQ(feed_parse(empty, strlen(empty), &feed), 0);
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    int page = 0;
    CHECK_EQ(render(&page), 1);
    CHECK_EQ(count_hits(UI_TODAY_HIT_BRIEF), 0);
    CHECK_EQ(n_hits, 0);
    write_png("today_empty_p1.png");
    feed_free(&feed);
}

/* Long texts: every line is cut to fit, the headline to three lines. */
static void test_long_texts(void)
{
    char js[6000];
    char lng[400];
    memset(lng, 'W', 300);
    lng[300] = 0;
    snprintf(js, sizeof(js),
             "{\"v\":1,\"version\":1,\"briefing\":{\"date\":\"2026-10-19\",\"text\":\"x\"},"
             "\"calendar\":[{\"id\":\"e\",\"title\":\"%s\",\"where\":\"%s\",\"start\":\"2026-10-20T12:00:00Z\"}],"
             "\"todos\":[{\"id\":\"n\",\"due\":\"2026-10-20\",\"items\":[{\"i\":0,\"text\":\"%s\"},"
             "{\"i\":1,\"text\":\"word word word word word word word word word word word word word word\"}]}],"
             "\"insight\":{\"date\":\"2026-10-20\",\"headline\":\"%s %s\","
             "\"focus\":[{\"ref\":\"todo:n:0\",\"why\":\"%s\"},{\"ref\":\"todo:n:1\",\"why\":\"%s\"}]}}",
             lng, lng, lng, "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four twenty-five twenty-six",
             "and some more words so it needs well over three lines on the panel, which is cut", lng, lng);
    CHECK_EQ(feed_parse(js, strlen(js), &feed), 0);
    today_plan_for_today(&feed, TODAY, NOW, TZ, &plan);
    CHECK(plan.from_insight);
    int page = 0;
    CHECK_EQ(render(&page), 1);
    write_png("today_long_p1.png");
    feed_free(&feed);
}

int main(void)
{
    test_insight();
    test_no_insight();
    test_long_texts();
    return checks_done("test_ui_today");
}
