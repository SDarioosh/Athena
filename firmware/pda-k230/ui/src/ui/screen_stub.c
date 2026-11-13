/* Placeholders for the tabs not built yet (Brief, Calendar, Talks, Status): the title and a
 * short note. Each goes when its screen_<name>.c exists (README.md "Adding a screen").
 * Also ui_no_feed(), shared by every screen. */
#include "app_state.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

typedef struct {
    const char *title, *icon, *text;
} stub_t;

static void stub_build(lv_obj_t *page, const stub_t *s)
{
    lv_obj_t *scroll = ui_page_scroller(page);
    ui_page_title(scroll, s->title, NULL);
    ui_empty_state(scroll, s->icon, C_TEXT_2, s->title, s->text);
}

#define STUB(name, title_, icon_, text_)                                                   \
    static const stub_t name##_stub = {title_, icon_, text_};                              \
    static void name##_build(lv_obj_t *page) { stub_build(page, &name##_stub); }           \
    const ui_screen_t screen_##name = {.label = title_, .icon = icon_, .build = name##_build}

STUB(talks, "Talks", ICON_MESSAGES_SQUARE, "Recent conversations, summaries and transcripts come here.");
STUB(status, "Status", ICON_ACTIVITY, "Sync, pairing, Tailscale and settings come here.");

lv_obj_t *ui_no_feed(lv_obj_t *parent)
{
    const char *text;
    if (!app_link_up()) {
        text = "The sync service isn't running yet.";
    } else {
        switch (app_net()->state) {
        case APP_NET_UNPAIRED: text = "Pair this PDA with Odysseus on the Status tab."; break;
        case APP_NET_OFFLINE: text = "Offline. It syncs as soon as the hub is reachable."; break;
        case APP_NET_AUTH_FAILED: text = "The hub refused the token. Pair again on the Status tab."; break;
        default: text = "Waiting for the first sync\xe2\x80\xa6"; break;
        }
    }
    return ui_empty_state(parent, ICON_INBOX, C_TEXT_2, "No feed yet", text);
}
