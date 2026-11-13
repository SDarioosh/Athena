/* Status (T5 plan §5 Status, docs/k230-pda-plan.md "Screens": Settings in Status), in
 * grouped cards: CONNECTION (sync state from syncd's `net`, the hub, Wi-Fi, changes
 * waiting, Sync now), TAILSCALE (state and name from `tailnet`; the login link as a QR
 * code), PAIRING (pair, unpair), DISPLAY (brightness, the ambient and off timeouts),
 * HANDS-FREE (the switch, presence wake, the camera, a live gesture tester), DEVICE
 * (firmware, feed, clock, battery, LilyGO's launcher, wipe the cache).
 *
 * The page is rebuilt only when something it shows changed (a signature of the values), so
 * vision's 5-second `state` messages don't redraw it; the gesture tester updates in place. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "icons_extra.h"
#include "local_msg.h"
#include "pda_time.h"
#include "screens.h"
#include "talks.h"
#include "theme.h"
#include "ui_internal.h" /* ui_hooks(): the brightness preview while the slider moves */
#include "ui_kit.h"
#include "widgets.h"
#include "wifi_info.h"

#define WIFI_EVERY_MS 10000

/* what the page shows, to tell whether a rebuild is needed */
typedef struct {
    bool link, have_feed, time_valid, charging, hands_free, presence, cam_connected;
    app_net_state_t net;
    app_tailnet_state_t tail;
    app_camera_t camera;
    int fps, brightness, dim, off, battery, outbox, tz;
    uint32_t feed_version;
    char hub[128], net_msg[128], tail_name[128], tail_url[256], synced[64], seen[48], clock[12], cam_msg[96];
    wifi_info_t wifi;
} sig_t;

static struct {
    lv_obj_t *scroll;
    sig_t sig;
    bool have_sig;
    bool practice; /* gestures only show in the tester */
    bool qr_open;  /* the Tailscale login sheet */
    wifi_info_t wifi;
    int64_t wifi_at;
    /* the gesture tester */
    lv_obj_t *g_icon, *g_name, *g_meta, *g_shape, *g_frame, *g_dot;
    vision_gesture_t g;
    double conf;
    int64_t g_at_ms;
    vision_shape_t shape;
    double hx, hy;
    bool hand;
} S;

/* ---- words ---- */

/* "Just now" / "Today 09:00" / "Yesterday 18:00" read on in lower case after "Synced " */
static void lower_lead(char *rel)
{
    if (strncmp(rel, "Just", 4) == 0 || strncmp(rel, "Today", 5) == 0 || strncmp(rel, "Yesterday", 9) == 0 ||
        strncmp(rel, "Tomorrow", 8) == 0) {
        rel[0] = (char)(rel[0] - 'A' + 'a');
    }
}

static void synced_words(int64_t last_ok, char *out, size_t cap)
{
    if (last_ok <= 0) {
        snprintf(out, cap, "Not synced yet");
        return;
    }
    char rel[48];
    talks_rel_time(last_ok, app_now(), app_tz_min(), rel, sizeof(rel));
    lower_lead(rel);
    snprintf(out, cap, "Synced %s", rel);
}

static const char *gesture_words(vision_gesture_t g)
{
    switch (g) {
    case VIS_G_SWIPE_LEFT: return "Swipe left";
    case VIS_G_SWIPE_RIGHT: return "Swipe right";
    case VIS_G_SWIPE_UP: return "Swipe up";
    case VIS_G_SWIPE_DOWN: return "Swipe down";
    case VIS_G_PALM: return "Open palm";
    case VIS_G_FIST: return "Fist";
    case VIS_G_THUMBS_UP: return "Thumbs up";
    case VIS_G_POINT: return "Point";
    case VIS_G_NONE: break;
    }
    return "No gesture yet";
}

static const char *gesture_does(vision_gesture_t g)
{
    switch (g) {
    case VIS_G_SWIPE_LEFT: return "next tab";
    case VIS_G_SWIPE_RIGHT: return "previous tab";
    case VIS_G_SWIPE_UP: return "page down";
    case VIS_G_SWIPE_DOWN: return "page up";
    case VIS_G_PALM: return "back to Today";
    case VIS_G_FIST: return "screen off";
    case VIS_G_THUMBS_UP: return "held: ticks a to-do";
    case VIS_G_POINT: return "moves the focus";
    case VIS_G_NONE: break;
    }
    return "";
}

static void gesture_glyph(vision_gesture_t g, const lv_font_t **font, const char **glyph)
{
    *font = fx()->icon_l;
    *glyph = ICONX_POINTER;
    switch (g) {
    case VIS_G_SWIPE_LEFT: *glyph = ICONX_ARROW_LEFT; break;
    case VIS_G_SWIPE_RIGHT: *glyph = ICONX_ARROW_RIGHT; break;
    case VIS_G_SWIPE_UP: *glyph = ICONX_ARROW_UP; break;
    case VIS_G_SWIPE_DOWN: *font = F.icon_l; *glyph = ICON_ARROW_DOWN; break;
    case VIS_G_FIST: *glyph = ICONX_GRAB; break;
    case VIS_G_THUMBS_UP: *glyph = ICONX_THUMBS_UP; break;
    case VIS_G_POINT: *glyph = ICONX_POINTER; break;
    case VIS_G_PALM:
    case VIS_G_NONE: *font = F.icon_l; *glyph = ICON_HAND; break;
    }
}

static const char *shape_words(vision_shape_t s)
{
    switch (s) {
    case VIS_SHAPE_OPEN: return "open hand";
    case VIS_SHAPE_FIST: return "fist";
    case VIS_SHAPE_POINT: return "pointing";
    case VIS_SHAPE_THUMBS_UP: return "thumbs up";
    case VIS_SHAPE_OTHER: return "a hand";
    case VIS_SHAPE_NONE: break;
    }
    return "no hand";
}

/* ---- the gesture tester (updated in place) ---- */

#define FRAME_W 96
#define FRAME_H 128
#define DOT 18

static void tester_refresh(void)
{
    if (!S.g_icon) {
        return;
    }
    const lv_font_t *font;
    const char *glyph;
    gesture_glyph(S.g, &font, &glyph);
    lv_label_set_text(S.g_icon, glyph);
    lv_obj_set_style_text_font(S.g_icon, font, 0);
    lv_obj_set_style_text_color(S.g_icon, C(S.g ? C_ACCENT : C_TEXT_3), 0);
    ui_text_set(S.g_name, gesture_words(S.g));
    char meta[96];
    if (S.g) {
        char age[24];
        pda_fmt_age((app_mono_ms() - S.g_at_ms) / 1000, age, sizeof(age));
        snprintf(meta, sizeof(meta), "%d %% \xc2\xb7 %s \xc2\xb7 %s", (int)(S.conf * 100 + 0.5), age,
                 gesture_does(S.g));
    } else {
        snprintf(meta, sizeof(meta), "Swipe, point, or show a palm, a fist or a thumbs-up.");
    }
    ui_text_set(S.g_meta, meta);
    char sh[48];
    snprintf(sh, sizeof(sh), "Now: %s", S.hand ? shape_words(S.shape) : "no hand");
    ui_text_set(S.g_shape, sh);
    lv_obj_set_style_text_color(S.g_shape, C(S.hand ? C_TEXT : C_TEXT_3), 0);
    if (S.hand) {
        lv_obj_set_pos(S.g_dot, (int32_t)(S.hx * (FRAME_W - 4 - DOT)), (int32_t)(S.hy * (FRAME_H - 4 - DOT)));
    }
    ui_show(S.g_dot, S.hand);
}

static void tester_deleted(lv_event_t *e)
{
    (void)e;
    S.g_icon = S.g_name = S.g_meta = S.g_shape = S.g_frame = S.g_dot = NULL;
}

bool status_vision_event(const vision_event_t *ev)
{
    if (ev->type == VIS_GESTURE) {
        S.g = ev->g;
        S.conf = ev->conf;
        S.g_at_ms = app_mono_ms();
    } else if (ev->type == VIS_HAND) {
        S.hand = ev->shape != VIS_SHAPE_NONE;
        S.shape = ev->shape;
        S.hx = ev->x < 0 ? 0 : ev->x > 1 ? 1 : ev->x;
        S.hy = ev->y < 0 ? 0 : ev->y > 1 ? 1 : ev->y;
    } else {
        return false;
    }
    tester_refresh();
    if (S.practice && !ui_tab_visible(UI_TAB_STATUS)) {
        S.practice = false; /* left the page: gestures work again */
    }
    return S.practice && app_settings()->hands_free;
}

static void practice_changed(lv_event_t *e)
{
    S.practice = lv_obj_has_state(lv_event_get_target(e), LV_STATE_CHECKED);
}

static void tester(lv_obj_t *card)
{
    lv_obj_t *b = kit_card_block(card, SP_3);
    lv_obj_set_style_pad_top(b, SP_2, 0);
    lv_obj_t *panel = kit_row(b, SP_4);
    lv_obj_set_width(panel, LV_PCT(100));
    lv_obj_set_style_radius(panel, 22, 0);
    lv_obj_set_style_bg_color(panel, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(panel, SP_4, 0);
    lv_obj_add_event_cb(panel, tester_deleted, LV_EVENT_DELETE, NULL);

    lv_obj_t *ring = lv_obj_create(panel);
    lv_obj_remove_style_all(ring);
    lv_obj_remove_flag(ring, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(ring, 88, 88);
    lv_obj_set_style_radius(ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ring, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(ring, LV_OPA_COVER, 0);
    S.g_icon = kit_icon(ring, F.icon_l, ICON_HAND, C_TEXT_3);
    lv_obj_center(S.g_icon);

    const int32_t w = UI_CARD_INNER_W - 2 * SP_4 - 88 - FRAME_W - 2 * SP_4;
    lv_obj_t *col = kit_col(panel, SP_1);
    lv_obj_set_width(col, w);
    S.g_name = ui_text(col, F.body_b, C_TEXT, "", w, 1);
    S.g_meta = ui_text(col, F.caption, C_TEXT_2, "", w, 3);
    S.g_shape = ui_text(col, F.caption, C_TEXT_3, "", w, 1);

    /* where the hand is (mirrored, like the cursor) */
    S.g_frame = lv_obj_create(panel);
    lv_obj_remove_style_all(S.g_frame);
    lv_obj_remove_flag(S.g_frame, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(S.g_frame, FRAME_W, FRAME_H);
    lv_obj_set_style_radius(S.g_frame, 14, 0);
    lv_obj_set_style_border_color(S.g_frame, C(C_SURFACE_3), 0);
    lv_obj_set_style_border_width(S.g_frame, 2, 0);
    S.g_dot = lv_obj_create(S.g_frame);
    lv_obj_remove_style_all(S.g_dot);
    lv_obj_remove_flag(S.g_dot, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(S.g_dot, DOT, DOT);
    lv_obj_set_style_radius(S.g_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(S.g_dot, C(C_ACCENT), 0);
    lv_obj_set_style_bg_opa(S.g_dot, LV_OPA_COVER, 0);
    lv_obj_add_flag(S.g_dot, LV_OBJ_FLAG_HIDDEN);
    tester_refresh();
}

/* ---- actions ---- */

static void sync_clicked(lv_event_t *e)
{
    (void)e;
    ui_request_sync();
}

static void pair_clicked(lv_event_t *e)
{
    (void)e;
    pairing_open();
}

static void do_unpair(void)
{
    char buf[64];
    const size_t n = local_unpair(buf, sizeof(buf));
    if (!app_link_up() || ui_send_local(buf, n) != 0) {
        ui_toast(UI_TOAST_ERROR, "The sync service isn't running");
        return;
    }
    ui_toast(UI_TOAST_OK, "Unpaired");
}

static void unpair_clicked(lv_event_t *e)
{
    (void)e;
    kit_confirm("Unpair this PDA?",
                "It stops syncing until you pair it again. The feed and the waiting changes stay on "
                "the device. To cut it off for good, also revoke its token in Odysseus.",
                fx()->icon_l, ICONX_UNLINK, "Unpair", true, do_unpair);
}

static void do_launcher(void)
{
    ui_start_launcher();
}

static void launcher_clicked(lv_event_t *e)
{
    (void)e;
    kit_confirm("Open LilyGO's launcher?",
                "Athena PDA closes; syncing and the camera stop. It starts again at the next boot.",
                fx()->icon_l, ICONX_APP_WINDOW, "Open the launcher", false, do_launcher);
}

static void do_wipe(void)
{
    if (app_wipe() == 0) {
        ui_toast(UI_TOAST_OK, "Feed, details and outbox wiped");
    } else {
        ui_toast(UI_TOAST_ERROR, "Couldn't wipe everything");
    }
}

static void set_hands_free(lv_event_t *e)
{
    app_settings_t s = *app_settings();
    s.hands_free = lv_obj_has_state(lv_event_get_target(e), LV_STATE_CHECKED);
    if (!s.hands_free) {
        S.practice = false;
    }
    app_set_settings(&s);
}

static void set_presence(lv_event_t *e)
{
    app_settings_t s = *app_settings();
    s.presence_wake = lv_obj_has_state(lv_event_get_target(e), LV_STATE_CHECKED);
    app_set_settings(&s);
}

static const int DIM_S[] = {30, 60, 120, 300, 0};
static const char *const DIM_L[] = {"30 s", "1 min", "2 min", "5 min", "Never"};
static const int OFF_S[] = {300, 900, 3600, 0};
static const char *const OFF_L[] = {"5 min", "15 min", "1 h", "Never"};

static int pick(const int *v, int n, int cur)
{
    for (int i = 0; i < n; i++) {
        if (v[i] == cur) {
            return i;
        }
    }
    return -1;
}

static void dim_clicked(lv_event_t *e)
{
    const int i = kit_choice_index(lv_event_get_current_target(e));
    app_settings_t s = *app_settings();
    s.dim_after_s = DIM_S[i];
    app_set_settings(&s);
}

static void off_clicked(lv_event_t *e)
{
    const int i = kit_choice_index(lv_event_get_current_target(e));
    app_settings_t s = *app_settings();
    s.off_after_s = OFF_S[i];
    app_set_settings(&s);
}

static void bright_event(lv_event_t *e)
{
    lv_obj_t *sl = lv_event_get_target(e);
    lv_obj_t *val = lv_event_get_user_data(e);
    const int v = (int)lv_slider_get_value(sl);
    lv_label_set_text_fmt(val, "%d %%", v);
    if (lv_event_get_code(e) == LV_EVENT_VALUE_CHANGED) {
        if (ui_hooks()->brightness) {
            ui_hooks()->brightness(v); /* live; saved on release */
        }
    } else if (v != app_settings()->brightness) {
        app_settings_t s = *app_settings();
        s.brightness = v;
        app_set_settings(&s);
    }
}

/* ---- the QR sheet: the Tailscale login link ---- */

#define QR_BOX 400
#define QR_SIZE 344

static void qr_build(lv_obj_t *s, void *arg)
{
    (void)arg;
    const int32_t w = UI_W - 2 * (UI_MARGIN + SP_2);
    lv_obj_t *t = ui_text(s, F.title, C_TEXT, "Log in to Tailscale", w, 2);
    lv_obj_set_style_text_line_space(t, 0, 0);
    ui_text(s, F.body, C_TEXT_2, "Scan the code with your phone, sign in, and approve this PDA in your tailnet.",
            w, 4);
    const app_tailnet_t *tn = app_tailnet();
    lv_obj_t *box = lv_obj_create(s);
    lv_obj_remove_style_all(box);
    lv_obj_remove_flag(box, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(box, QR_BOX, QR_BOX);
    lv_obj_set_style_radius(box, 28, 0);
    lv_obj_set_style_bg_color(box, C(C_TEXT), 0); /* the quiet zone round the code */
    lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
    lv_obj_set_style_margin_left(box, (w - QR_BOX) / 2, 0);
    lv_obj_set_style_margin_ver(box, SP_2, 0);
    if (tn->url[0]) {
        lv_obj_t *qr = lv_qrcode_create(box);
        lv_qrcode_set_size(qr, QR_SIZE);
        lv_qrcode_set_dark_color(qr, C(C_BG));
        lv_qrcode_set_light_color(qr, C(C_TEXT));
        lv_qrcode_update(qr, tn->url, (uint32_t)strlen(tn->url));
        lv_obj_center(qr);
        lv_obj_t *u = ui_text(s, F.caption, C_TEXT_3, tn->url, w, 2);
        lv_obj_set_style_text_align(u, LV_TEXT_ALIGN_CENTER, 0);
    } else {
        lv_obj_t *l = ui_text(box, F.body, C_BG, "Waiting for the login link\xe2\x80\xa6", QR_SIZE, 2);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_center(l);
    }
}

static void qr_closed(void *arg)
{
    (void)arg;
    S.qr_open = false;
}

static void qr_clicked(lv_event_t *e)
{
    (void)e;
    S.qr_open = true;
    ui_sheet_open(qr_build, NULL, qr_closed);
}

/* ---- the cards ---- */

static lv_obj_t *card_with_head(const char *label)
{
    lv_obj_t *card = ui_card(S.scroll);
    ui_section_head(card, NULL, label, C_TEXT_2, NULL, NULL, NULL);
    return card;
}

static lv_obj_t *buttons(lv_obj_t *card)
{
    lv_obj_t *b = kit_card_block(card, SP_3);
    lv_obj_set_style_pad_top(b, SP_2, 0);
    lv_obj_set_style_pad_bottom(b, UI_CARD_PAD - SP_2, 0);
    return b;
}

static void connection_card(void)
{
    const app_net_t *n = app_net();
    lv_obj_t *card = card_with_head("CONNECTION");
    kit_state_t st;
    kit_state_line(card, &st);
    lv_obj_set_style_pad_top(st.box, 0, 0);
    char synced[64], sub[256];
    synced_words(n->last_ok, synced, sizeof(synced));
    if (!app_link_up()) {
        const int64_t seen = app_last_link_s();
        char rel[48] = "";
        if (seen > 0) {
            talks_rel_time(seen, app_now(), app_tz_min(), rel, sizeof(rel));
            lower_lead(rel);
        }
        snprintf(sub, sizeof(sub), "athena-pda-syncd isn't connected. It restarts by itself.%s%s%s",
                 rel[0] ? " Last seen " : "", rel, rel[0] ? "." : "");
        kit_state_set(&st, F.icon, ICON_CLOUD_OFF, C_TEXT_2, "Sync service not running", sub);
    } else {
        switch (n->state) {
        case APP_NET_UNKNOWN:
            kit_state_set(&st, F.icon, ICON_LOADER, C_TEXT_2, "Starting\xe2\x80\xa6", "Waiting for the sync service.");
            break;
        case APP_NET_UNPAIRED:
            kit_state_set(&st, fx()->icon, ICONX_UNLINK, C_TEXT_2, "Not paired",
                          "Pair with Odysseus below to start syncing.");
            break;
        case APP_NET_OFFLINE:
            snprintf(sub, sizeof(sub), "%s. %s.", n->msg[0] ? n->msg : "Odysseus isn't reachable", synced);
            kit_state_set(&st, F.icon, ICON_WIFI_OFF, C_DANGER, "Offline", sub);
            break;
        case APP_NET_SYNCING: kit_state_set(&st, F.icon, ICON_REFRESH_CW, C_ACCENT, "Syncing\xe2\x80\xa6", synced); break;
        case APP_NET_OK:
            snprintf(sub, sizeof(sub), "%s%s%s", synced, n->msg[0] ? " \xc2\xb7 " : "", n->msg);
            kit_state_set(&st, F.icon, ICON_CHECK, C_ACCENT, "Up to date", sub);
            break;
        case APP_NET_AUTH_FAILED:
            kit_state_set(&st, F.icon, ICON_CIRCLE_ALERT, C_DANGER, "Odysseus refused this PDA",
                          "Its token was revoked or the hub was reset. Pair it again below.");
            break;
        }
    }
    kit_rule(card);
    kit_value_row(card, fx()->icon_s, ICONX_GLOBE, "Odysseus", n->hub[0] ? n->hub : "Not set");
    kit_rule(card);
    char wifi[96];
    if (!S.wifi.present) {
        snprintf(wifi, sizeof(wifi), "No Wi-Fi device");
    } else if (!S.wifi.up) {
        snprintf(wifi, sizeof(wifi), "Not connected");
    } else {
        snprintf(wifi, sizeof(wifi), "%s%s%s", S.wifi.have_level ? wifi_signal_word(S.wifi.level_dbm) : "Connected",
                 S.wifi.ip[0] ? " \xc2\xb7 " : "", S.wifi.ip);
    }
    kit_value_row(card, F.icon_s, S.wifi.up ? ICON_WIFI : ICON_WIFI_OFF, "Wi-Fi", wifi);
    kit_rule(card);
    const int ob = app_outbox_count();
    char obs[32];
    snprintf(obs, sizeof(obs), ob == 0 ? "Nothing" : ob == 1 ? "1 change" : "%d changes", ob);
    kit_value_row(card, F.icon_s, ICON_UPLOAD, "Waiting to sync", obs);
    lv_obj_t *b = buttons(card);
    lv_obj_t *sb = kit_button(b, KIT_SECONDARY, F.icon_s, ICON_REFRESH_CW, "Sync now", sync_clicked, NULL);
    kit_button_set_enabled(sb, app_link_up() && n->state != APP_NET_UNPAIRED && n->state != APP_NET_SYNCING &&
                                   n->state != APP_NET_AUTH_FAILED);
    ui_text(b, F.caption, C_TEXT_3, "Wi-Fi networks are added over adb with athena-wifi; this screen only shows the link.",
            UI_CARD_INNER_W, 2);
}

static void tailscale_card(void)
{
    const app_tailnet_t *t = app_tailnet();
    lv_obj_t *card = card_with_head("TAILSCALE");
    if (t->state == APP_TAILNET_NEEDS_LOGIN) {
        kit_state_t st;
        kit_state_line(card, &st);
        lv_obj_set_style_pad_top(st.box, 0, 0);
        kit_state_set(&st, fx()->icon, ICONX_QR_CODE, C_ACCENT, "Log in to Tailscale",
                      "This PDA reaches Odysseus over your tailnet. Scan the login link with your phone.");
        lv_obj_t *b = buttons(card);
        kit_button(b, KIT_PRIMARY, fx()->icon_s, ICONX_QR_CODE, "Show the login code", qr_clicked, NULL);
        return;
    }
    const char *state = t->state == APP_TAILNET_RUNNING   ? "Connected"
                        : t->state == APP_TAILNET_STOPPED ? "Stopped"
                                                          : "Unknown";
    lv_obj_t *v = kit_value_row(card, fx()->icon_s, ICONX_NETWORK, "State", state);
    lv_obj_set_style_text_color(v, C(t->state == APP_TAILNET_RUNNING ? C_ACCENT
                                     : t->state == APP_TAILNET_STOPPED ? C_DANGER
                                                                       : C_TEXT_2),
                                0);
    kit_rule(card);
    kit_value_row(card, fx()->icon_s, ICONX_SHIELD_CHECK, "This PDA", t->name[0] ? t->name : "\xe2\x80\x94");
    lv_obj_t *b = buttons(card);
    lv_obj_set_style_pad_top(b, 0, 0);
    ui_text(b, F.caption, C_TEXT_3,
            t->state == APP_TAILNET_STOPPED ? "tailscaled isn't running. It starts at boot; see athena-tailnet status."
                                            : "Only outgoing connections: no Funnel, Serve or exit node.",
            UI_CARD_INNER_W, 2);
}

static void pairing_card(void)
{
    const app_net_t *n = app_net();
    lv_obj_t *card = card_with_head("PAIRING");
    kit_state_t st;
    kit_state_line(card, &st);
    lv_obj_set_style_pad_top(st.box, 0, 0);
    const bool known = app_link_up() && n->state != APP_NET_UNKNOWN;
    const bool paired = known && n->state != APP_NET_UNPAIRED;
    char sub[200];
    if (!known) {
        kit_state_set(&st, fx()->icon, ICONX_LINK, C_TEXT_2, "Unknown", "The sync service knows; it isn't connected.");
    } else if (n->state == APP_NET_AUTH_FAILED) {
        snprintf(sub, sizeof(sub), "%s refused the token. Pair again.", n->hub[0] ? n->hub : "Odysseus");
        kit_state_set(&st, fx()->icon, ICONX_UNLINK, C_DANGER, "Pairing lost", sub);
    } else if (paired) {
        snprintf(sub, sizeof(sub), "%s", n->hub[0] ? n->hub : "Odysseus");
        kit_state_set(&st, fx()->icon, ICONX_LINK, C_ACCENT, "Paired with Odysseus", sub);
    } else {
        kit_state_set(&st, fx()->icon, ICONX_UNLINK, C_TEXT_2, "Not paired",
                      "Use the QR code from Odysseus' Pair a phone, or type its code.");
    }
    lv_obj_t *b = buttons(card);
    if (paired && n->state != APP_NET_AUTH_FAILED) {
        kit_button(b, KIT_QUIET, fx()->icon_s, ICONX_LINK, "Pair again", pair_clicked, NULL);
        kit_button(b, KIT_DANGER, fx()->icon_s, ICONX_UNLINK, "Unpair", unpair_clicked, NULL);
    } else {
        kit_button(b, KIT_PRIMARY, fx()->icon_s, ICONX_LINK, "Pair with Odysseus", pair_clicked, NULL);
    }
}

static void display_card(void)
{
    const app_settings_t *s = app_settings();
    lv_obj_t *card = card_with_head("DISPLAY");
    /* brightness: dim icon, slider, bright icon, the value */
    lv_obj_t *r = kit_row(card, SP_4);
    lv_obj_set_width(r, LV_PCT(100));
    lv_obj_set_style_pad_hor(r, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_ver(r, SP_3, 0);
    lv_obj_set_style_min_height(r, UI_TOUCH_MIN, 0);
    kit_icon(r, fx()->icon_s, ICONX_SUN_DIM, C_TEXT_2);
    lv_obj_t *sl = lv_slider_create(r);
    lv_obj_set_flex_grow(sl, 1);
    lv_obj_set_height(sl, 12);
    lv_obj_set_ext_click_area(sl, 30); /* a 72 px target round the thin track */
    lv_slider_set_range(sl, 5, 100);
    lv_slider_set_value(sl, s->brightness, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(sl, C(C_SURFACE_3), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(sl, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(sl, C(C_ACCENT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(sl, C(C_TEXT), LV_PART_KNOB);
    lv_obj_set_style_pad_all(sl, 12, LV_PART_KNOB);
    lv_obj_set_style_shadow_width(sl, 0, LV_PART_KNOB);
    lv_obj_set_style_anim_duration(sl, UI_ANIM_FAST, 0);
    ui_focusable(sl, false);
    kit_icon(r, F.icon_s, ICON_SUN, C_TEXT);
    lv_obj_t *val = ui_text(r, F.small_b, C_TEXT_2, "", 76, 0);
    lv_obj_set_style_text_align(val, LV_TEXT_ALIGN_RIGHT, 0);
    lv_label_set_text_fmt(val, "%d %%", s->brightness);
    lv_obj_add_event_cb(sl, bright_event, LV_EVENT_VALUE_CHANGED, val);
    lv_obj_add_event_cb(sl, bright_event, LV_EVENT_RELEASED, val);
    kit_rule(card);
    kit_choice_row(card, "Dim to the clock after", DIM_L, 5, pick(DIM_S, 5, s->dim_after_s), dim_clicked, NULL);
    kit_rule(card);
    lv_obj_t *off = kit_choice_row(card, "Then turn the screen off after", OFF_L, 4,
                                   pick(OFF_S, 4, s->off_after_s), off_clicked, NULL);
    lv_obj_set_style_pad_bottom(off, UI_CARD_PAD, 0);
}

static void handsfree_card(void)
{
    const app_settings_t *s = app_settings();
    const app_vision_t *v = app_vision();
    lv_obj_t *card = card_with_head("HANDS-FREE");
    lv_obj_set_style_pad_top(kit_switch_row(card, "Hands-free",
                                            "Control the PDA with hand gestures. Uses the camera; "
                                            "nothing leaves the device.",
                                            s->hands_free, set_hands_free, NULL),
                             0, 0);
    kit_rule(card);
    kit_switch_row(card, "Wake when someone is near",
                   "The camera notices a person: the screen wakes, and dims to the clock when they leave.",
                   s->presence_wake, set_presence, NULL);
    kit_rule(card);
    char cam[128];
    uint32_t col = C_TEXT_2;
    if (!v->connected) {
        snprintf(cam, sizeof(cam), "Service not running");
    } else if (v->camera == APP_CAMERA_ON) {
        snprintf(cam, sizeof(cam), "On \xc2\xb7 %d fps", (int)(v->fps + 0.5));
        col = C_CAMERA;
    } else if (v->camera == APP_CAMERA_ERROR) {
        snprintf(cam, sizeof(cam), "Error%s%s", v->msg[0] ? ": " : "", v->msg);
        col = C_DANGER;
    } else {
        snprintf(cam, sizeof(cam), s->hands_free || s->presence_wake ? "Starting\xe2\x80\xa6" : "Off");
    }
    lv_obj_t *cv = kit_value_row(card, F.icon_s, ICON_CAMERA, "Camera", cam);
    lv_obj_set_style_text_color(cv, C(col), 0);
    if (s->hands_free) {
        kit_rule(card);
        lv_obj_t *head = kit_card_block(card, 0);
        lv_obj_set_style_pad_top(head, SP_4, 0);
        lv_obj_set_style_pad_bottom(head, 0, 0);
        ui_text(head, F.body, C_TEXT, "Gesture tester", UI_CARD_INNER_W, 1);
        tester(card);
        lv_obj_t *p = kit_switch_row(card, "Practice here",
                                     "Gestures only show in the tester while this page is open.",
                                     S.practice, practice_changed, NULL);
        lv_obj_set_style_pad_bottom(lv_obj_get_parent(p), UI_CARD_PAD, 0);
    } else {
        lv_obj_t *b = buttons(card);
        lv_obj_set_style_pad_top(b, 0, 0);
        ui_text(b, F.caption, C_TEXT_3, "Turn Hands-free on to try the gestures here.", UI_CARD_INNER_W, 2);
    }
}

static void device_card(void)
{
    const feed_t *f = app_feed();
    lv_obj_t *card = card_with_head("DEVICE");
    kit_value_row(card, fx()->icon_s, ICONX_INFO, "Firmware", "athena-pda " APP_FW_VERSION);
    kit_rule(card);
    char line[128];
    if (f && f->generated) {
        char d[24], t[12];
        pda_fmt_date(f->generated, app_tz_min(), d, sizeof(d));
        pda_fmt_hm(f->generated, app_tz_min(), t, sizeof(t));
        snprintf(line, sizeof(line), "Built %s %s", d, t);
    } else {
        snprintf(line, sizeof(line), f ? "Version %u" : "None yet", f ? (unsigned)f->version : 0u);
    }
    kit_value_row(card, F.icon_s, ICON_INBOX, "Feed", line);
    kit_rule(card);
    const int tz = app_tz_min(), z = tz < 0 ? -tz : tz;
    if (app_time_valid()) {
        char d[24], hm[12];
        pda_fmt_date(app_now(), tz, d, sizeof(d));
        pda_fmt_hm(app_now(), tz, hm, sizeof(hm));
        snprintf(line, sizeof(line), "%s, %s", d, hm);
    } else {
        snprintf(line, sizeof(line), "Not set (waiting for NTP)");
    }
    kit_value_row(card, F.icon_s, ICON_CLOCK, "Clock", line);
    kit_rule(card);
    char off[16];
    if (z % 60) {
        snprintf(off, sizeof(off), "UTC%c%d:%02d", tz < 0 ? '-' : '+', z / 60, z % 60);
    } else {
        snprintf(off, sizeof(off), "UTC%c%d", tz < 0 ? '-' : '+', z / 60);
    }
    snprintf(line, sizeof(line), "%s%s%s", f && f->tz ? f->tz : "", f && f->tz ? ", " : "", off);
    kit_value_row(card, fx()->icon_s, ICONX_GLOBE, "Time zone", line);
    kit_rule(card);
    const int pct = app_battery_pct();
    if (pct >= 0) {
        snprintf(line, sizeof(line), "%d %%%s", pct, app_charging() ? " \xc2\xb7 charging" : "");
    } else {
        snprintf(line, sizeof(line), "Unknown");
    }
    kit_value_row(card, F.icon_s, app_charging() ? ICON_BATTERY_CHARGING : ICON_BATTERY_MEDIUM, "Battery", line);
    lv_obj_t *b = buttons(card);
    lv_obj_set_style_pad_top(b, SP_4, 0);
    kit_button(b, KIT_QUIET, fx()->icon_s, ICONX_APP_WINDOW, "Open LilyGO's launcher", launcher_clicked, NULL);
    kit_hold_button(b, fx()->icon_s, ICONX_TRASH_2, "Hold to wipe the cache", 1500, do_wipe);
    ui_text(b, F.caption, C_TEXT_3, "Wiping deletes the feed, the transcripts and the changes waiting to sync.",
            UI_CARD_INNER_W, 2);
}

/* ---- the page ---- */

/* the Wi-Fi state, read at most every WIFI_EVERY_MS */
static void wifi_now(void)
{
    if (!S.wifi_at || app_mono_ms() - S.wifi_at >= WIFI_EVERY_MS) {
        wifi_read("/proc/net/wireless", "/sys/class/net", &S.wifi);
        S.wifi_at = app_mono_ms();
    }
}

static void signature(sig_t *g)
{
    memset(g, 0, sizeof(*g));
    const app_net_t *n = app_net();
    const app_tailnet_t *t = app_tailnet();
    const app_vision_t *v = app_vision();
    const app_settings_t *s = app_settings();
    const feed_t *f = app_feed();
    g->link = app_link_up();
    g->net = n->state;
    snprintf(g->hub, sizeof(g->hub), "%s", n->hub);
    snprintf(g->net_msg, sizeof(g->net_msg), "%s", n->msg);
    synced_words(n->last_ok, g->synced, sizeof(g->synced));
    if (!g->link) {
        talks_rel_time(app_last_link_s(), app_now(), app_tz_min(), g->seen, sizeof(g->seen));
    }
    g->tail = t->state;
    snprintf(g->tail_name, sizeof(g->tail_name), "%s", t->name);
    snprintf(g->tail_url, sizeof(g->tail_url), "%s", t->url);
    g->cam_connected = v->connected;
    g->camera = v->camera;
    g->fps = (int)(v->fps + 0.5);
    snprintf(g->cam_msg, sizeof(g->cam_msg), "%s", v->msg);
    g->hands_free = s->hands_free;
    g->presence = s->presence_wake;
    g->brightness = s->brightness;
    g->dim = s->dim_after_s;
    g->off = s->off_after_s;
    g->battery = app_battery_pct();
    g->charging = app_charging();
    g->outbox = app_outbox_count();
    g->have_feed = f != NULL;
    g->feed_version = f ? f->version : 0;
    g->tz = app_tz_min();
    g->time_valid = app_time_valid();
    pda_fmt_hm(app_now(), app_tz_min(), g->clock, sizeof(g->clock));
    g->wifi = S.wifi;
}

static void rebuild(void)
{
    const int32_t y = lv_obj_get_scroll_y(S.scroll);
    lv_obj_clean(S.scroll);
    ui_page_title(S.scroll, "Status", NULL);
    connection_card();
    tailscale_card();
    pairing_card();
    display_card();
    handsfree_card();
    device_card();
    lv_obj_update_layout(S.scroll);
    lv_obj_scroll_to_y(S.scroll, y > 0 ? y : 0, LV_ANIM_OFF);
}

static void build(lv_obj_t *page)
{
    S.scroll = ui_page_scroller(page);
}

static void update(lv_obj_t *page, unsigned what)
{
    (void)page;
    /* the login sheet: closes itself once Tailscale is up */
    if (S.qr_open && app_tailnet()->state != APP_TAILNET_NEEDS_LOGIN) {
        ui_sheet_close();
        if (app_tailnet()->state == APP_TAILNET_RUNNING) {
            ui_toast(UI_TOAST_OK, "Tailscale is connected");
        }
    }
    if (!ui_tab_visible(UI_TAB_STATUS) && !(what & UI_DIRTY_SHOWN) && S.have_sig) {
        return; /* hidden: catch up when shown */
    }
    wifi_now();
    sig_t g;
    signature(&g);
    if (!S.have_sig || memcmp(&g, &S.sig, sizeof(g)) != 0) {
        S.sig = g;
        S.have_sig = true;
        rebuild();
    } else if (what & UI_DIRTY_CLOCK) {
        tester_refresh(); /* "2 min ago" */
    }
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    (void)page;
    return S.scroll;
}

const ui_screen_t screen_status = {
    .label = "Status",
    .icon = ICON_ACTIVITY,
    .build = build,
    .update = update,
    .scroller = scroller,
};
