/* Swipe navigation: tabs at the top level, back / forward inside detail screens. */
#include "check.h"
#include "ui_nav.h"

static ui_nav_t sw(ui_nav_in_t in, ui_nav_dir_t d)
{
    return ui_nav_swipe(&in, d);
}

int main(void)
{
    /* tabs wrap both ways */
    const ui_nav_in_t talks = {.tab = 4, .pages = 1, .sel = -1};
    ui_nav_t r = sw(talks, UI_NAV_LEFT);
    CHECK(r.kind == UI_NAV_TAB && r.arg == 5);
    r = sw(talks, UI_NAV_RIGHT);
    CHECK(r.kind == UI_NAV_TAB && r.arg == 3);
    const ui_nav_in_t status = {.tab = 5, .pages = 1, .sel = -1};
    CHECK_EQ(sw(status, UI_NAV_LEFT).arg, 0);
    const ui_nav_in_t today = {.tab = 0, .pages = 1, .sel = -1};
    CHECK_EQ(sw(today, UI_NAV_RIGHT).arg, 5);

    /* the bug seen on the board: a swipe left in a conversation opened from Talks went
     * to Brief (tab 1). In a detail screen it moves forward or does nothing. */
    const ui_nav_in_t conv = {.detail = true, .tab = 4, .page = 0, .pages = 1, .sel = 0, .n_items = 5};
    r = sw(conv, UI_NAV_LEFT);
    CHECK(r.kind == UI_NAV_ITEM && r.arg == 1);
    CHECK(sw(conv, UI_NAV_RIGHT).kind == UI_NAV_BACK);
    const ui_nav_in_t last_conv = {.detail = true, .tab = 4, .pages = 1, .sel = 4, .n_items = 5};
    CHECK(sw(last_conv, UI_NAV_LEFT).kind == UI_NAV_NONE);

    /* pages come first, then the transcript's next part */
    const ui_nav_in_t tr = {.detail = true, .page = 0, .pages = 3, .next_part = true, .sel = -1};
    r = sw(tr, UI_NAV_LEFT);
    CHECK(r.kind == UI_NAV_PAGE && r.arg == 1);
    const ui_nav_in_t tr_end = {.detail = true, .page = 2, .pages = 3, .next_part = true, .sel = -1};
    CHECK(sw(tr_end, UI_NAV_LEFT).kind == UI_NAV_NEXT_PART);
    const ui_nav_in_t tr_last = {.detail = true, .page = 2, .pages = 3, .sel = -1};
    CHECK(sw(tr_last, UI_NAV_LEFT).kind == UI_NAV_NONE);

    /* up/down page everywhere, clamped */
    r = sw(tr, UI_NAV_UP);
    CHECK(r.kind == UI_NAV_PAGE && r.arg == 1);
    CHECK(sw(tr, UI_NAV_DOWN).kind == UI_NAV_NONE);
    CHECK(sw(tr_last, UI_NAV_UP).kind == UI_NAV_NONE);
    r = sw(tr_last, UI_NAV_DOWN);
    CHECK(r.kind == UI_NAV_PAGE && r.arg == 1);
    return checks_done("test_ui_nav");
}
