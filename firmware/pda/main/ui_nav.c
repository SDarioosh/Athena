#include "ui_nav.h"

ui_nav_t ui_nav_swipe(const ui_nav_in_t *in, ui_nav_dir_t dir)
{
    const ui_nav_t none = {UI_NAV_NONE, 0};
    switch (dir) {
    case UI_NAV_UP:
        return in->page + 1 < in->pages ? (ui_nav_t){UI_NAV_PAGE, in->page + 1} : none;
    case UI_NAV_DOWN:
        return in->page > 0 ? (ui_nav_t){UI_NAV_PAGE, in->page - 1} : none;
    case UI_NAV_LEFT:
        if (!in->detail) {
            return (ui_nav_t){UI_NAV_TAB, (in->tab + 1) % UI_NAV_TABS};
        }
        if (in->page + 1 < in->pages) {
            return (ui_nav_t){UI_NAV_PAGE, in->page + 1};
        }
        if (in->next_part) {
            return (ui_nav_t){UI_NAV_NEXT_PART, 0};
        }
        if (in->sel >= 0 && in->sel + 1 < in->n_items) {
            return (ui_nav_t){UI_NAV_ITEM, in->sel + 1};
        }
        return none; /* at the end */
    case UI_NAV_RIGHT:
        if (!in->detail) {
            return (ui_nav_t){UI_NAV_TAB, (in->tab + UI_NAV_TABS - 1) % UI_NAV_TABS};
        }
        return (ui_nav_t){UI_NAV_BACK, 0};
    default:
        return none;
    }
}
