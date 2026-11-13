/* ui_no_feed(), shared by every screen: what a tab shows while there is no feed. (The
 * placeholder tabs that were here are gone: every tab has its screen_<name>.c.) */
#include "app_state.h"
#include "screens.h"
#include "theme.h"
#include "widgets.h"

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
