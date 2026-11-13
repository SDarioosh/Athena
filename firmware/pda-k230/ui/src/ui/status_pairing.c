/* Pair with Odysseus (pushed from Status): scan the hub's "Pair a phone" QR code with the
 * K230's camera, or type the address and the code. The flow is core/pairing.c's; this file
 * draws each stage, turns vision's `qr` mode on while scanning (app_set_qr_scan), sends
 * LOCAL `pair` (ui_send_local) and takes syncd's pair_result (an app listener). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "icons_extra.h"
#include "pairing.h"
#include "screens.h"
#include "text_entry_local.h"
#include "theme.h"
#include "ui_kit.h"
#include "widgets.h"

#define FINDER 380

typedef struct {
    pair_flow_t flow;
    lv_obj_t *page, *scroll, *kb, *addr, *code, *cam_dot, *cam_text, *scanline;
    lv_timer_t *timer;
    pair_stage_t built; /* the stage on screen */
    char addr_text[320], code_text[128]; /* what was typed, kept across rebuilds */
    bool typed;
    bool built_once;
    char hint_shown[PAIR_MSG_MAX];
} pair_ui_t;

static pair_ui_t *P; /* the open pairing screen, or NULL */
static bool s_rebuild_queued;

static void rebuild(void);

static void camera_follow(void)
{
    app_set_qr_scan(P && pair_wants_camera(&P->flow));
}

static void send(const char *json, size_t n)
{
    const bool ok = app_link_up() && ui_send_local(json, n) == 0;
    pair_sent(&P->flow, ok, app_mono_ms());
    camera_follow();
    rebuild();
}

/* ---- actions ---- */

static void scan_clicked(lv_event_t *e)
{
    (void)e;
    pair_start_scan(&P->flow);
    camera_follow();
    rebuild();
}

static void type_clicked(lv_event_t *e)
{
    (void)e;
    P->typed = false;
    pair_start_typing(&P->flow);
    camera_follow();
    rebuild();
}

static void back_clicked(lv_event_t *e)
{
    (void)e;
    if (pair_back(&P->flow)) {
        camera_follow();
        rebuild();
    }
}

static void done_clicked(lv_event_t *e)
{
    (void)e;
    ui_pop();
}

static void submit(void *ud)
{
    (void)ud;
    if (!P || P->flow.stage != PAIR_TYPING) {
        return;
    }
    char out[PAIR_LOCAL_MAX];
    size_t n;
    const pair_step_t s = pair_submit(&P->flow, te_text(P->addr), te_text(P->code), out, sizeof(out), &n);
    if (s == PAIR_SEND) {
        send(out, n);
    } else if (s == PAIR_REJECTED) {
        rebuild(); /* the hint under the fields */
    }
}

static void submit_clicked(lv_event_t *e)
{
    (void)e;
    submit(NULL);
}

bool pairing_vision_event(const vision_event_t *ev)
{
    if (!P || ev->type != VIS_QR) {
        return false;
    }
    char out[PAIR_LOCAL_MAX];
    size_t n;
    const pair_step_t s = pair_on_qr(&P->flow, ev->text, out, sizeof(out), &n);
    if (s == PAIR_SEND) {
        send(out, n);
    } else if (s == PAIR_REJECTED) {
        rebuild();
    }
    return true;
}

static void on_app(void *ctx, app_ev_t ev, int32_t arg)
{
    (void)ctx;
    (void)arg;
    if (!P) {
        return;
    }
    if (ev == APP_EV_PAIR_RESULT) {
        const char *msg;
        const bool ok = app_pair_result(&msg);
        if (pair_on_result(&P->flow, ok, msg)) {
            rebuild();
        }
    } else if (ev == APP_EV_VISION && P->flow.stage == PAIR_SCANNING && P->cam_text) {
        rebuild(); /* the camera indicator */
    }
}

static void tick(lv_timer_t *t)
{
    (void)t;
    if (P && pair_tick(&P->flow, app_mono_ms())) {
        rebuild();
    }
}

/* ---- drawing ---- */

static lv_obj_t *ring(lv_obj_t *parent, int32_t d, uint32_t color, const lv_font_t *font, const char *icon)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_remove_style_all(r);
    lv_obj_remove_flag(r, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(r, d, d);
    lv_obj_set_style_radius(r, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(r, lv_color_mix(C(color), C(C_BG), LV_OPA_20), 0);
    lv_obj_set_style_bg_opa(r, LV_OPA_COVER, 0);
    lv_obj_t *i = kit_icon(r, font, icon, color);
    lv_obj_center(i);
    return r;
}

static lv_obj_t *centered(lv_obj_t *parent, const lv_font_t *f, uint32_t color, const char *text, int lines)
{
    lv_obj_t *l = ui_text(parent, f, color, text, UI_CARD_W - 2 * SP_4, lines);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    return l;
}

static void hint_box(lv_obj_t *parent, const char *msg, uint32_t color)
{
    lv_obj_t *b = kit_row(parent, SP_3);
    lv_obj_set_width(b, LV_PCT(100));
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_radius(b, 20, 0);
    lv_obj_set_style_bg_color(b, lv_color_mix(C(color), C(C_BG), LV_OPA_20), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(b, SP_4, 0);
    lv_obj_t *i = lv_label_create(b);
    lv_label_set_text(i, ICON_CIRCLE_ALERT);
    lv_obj_set_style_text_font(i, F.small, 0);
    lv_obj_set_style_text_color(i, C(color), 0);
    lv_obj_set_style_pad_top(i, 2, 0);
    ui_text(b, F.small, C_TEXT, msg, UI_CARD_W - 2 * SP_4 - 26 - SP_3, 4);
}

static void option_card(lv_obj_t *parent, const char *icon, const char *title, const char *sub, lv_event_cb_t cb)
{
    lv_obj_t *card = ui_card(parent);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_all(card, UI_CARD_PAD, 0);
    lv_obj_set_style_pad_column(card, SP_5, 0);
    lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_color(card, C(C_SURFACE_2), LV_STATE_PRESSED);
    lv_obj_add_event_cb(card, cb, LV_EVENT_CLICKED, NULL);
    ui_focusable(card, false);
    ring(card, 88, C_ACCENT, fx()->icon_l, icon);
    const int32_t w = UI_CARD_INNER_W - 88 - SP_5 - 24 - SP_5;
    lv_obj_t *col = kit_col(card, SP_1);
    lv_obj_set_width(col, w);
    ui_text(col, F.body_b, C_TEXT, title, w, 2);
    ui_text(col, F.small, C_TEXT_2, sub, w, 3);
    kit_icon(card, F.icon_s, ICON_CHEVRON_RIGHT, C_TEXT_3);
}

static void build_choose(lv_obj_t *s)
{
    lv_obj_t *head = kit_col(s, SP_3);
    lv_obj_set_width(head, LV_PCT(100));
    lv_obj_set_style_pad_hor(head, SP_1, 0);
    lv_obj_set_style_pad_bottom(head, SP_2, 0);
    lv_obj_t *t = ui_text(head, F.title, C_TEXT, "Pair with Odysseus", UI_CARD_W, 2);
    lv_obj_set_style_text_line_space(t, 0, 0);
    ui_text(head, F.body, C_TEXT_2,
            "On Odysseus open Settings \xe2\x86\x92 Integrations \xe2\x86\x92 Pair a phone. It shows a "
            "QR code and a code to type. Then:",
            UI_CARD_W - SP_2, 5);
    option_card(s, ICONX_SCAN_QR_CODE, "Scan the QR code",
                "With this PDA's camera. The picture never leaves the device.", scan_clicked);
    option_card(s, ICONX_KEYBOARD, "Type the code", "The Odysseus address and the pairing code.", type_clicked);
    const app_net_t *n = app_net();
    if (app_link_up() && n->state != APP_NET_UNPAIRED && n->state != APP_NET_UNKNOWN) {
        char line[200];
        snprintf(line, sizeof(line), "This PDA is paired with %s now. Pairing again replaces it.",
                 n->hub[0] ? n->hub : "Odysseus");
        hint_box(s, line, C_TEXT_2);
    } else if (!app_link_up()) {
        hint_box(s, "The sync service isn't running, so pairing can't finish right now.", C_DANGER);
    }
}

static void scan_line_y(void *var, int32_t v)
{
    lv_obj_set_y(var, v);
}

static void corner(lv_obj_t *f, lv_align_t al, lv_border_side_t sides)
{
    lv_obj_t *c = lv_obj_create(f);
    lv_obj_remove_style_all(c);
    lv_obj_remove_flag(c, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(c, 72, 72);
    lv_obj_set_style_border_color(c, C(C_ACCENT), 0);
    lv_obj_set_style_border_width(c, 6, 0);
    lv_obj_set_style_border_side(c, sides, 0);
    lv_obj_set_style_radius(c, 28, 0);
    lv_obj_align(c, al, 0, 0);
}

static void build_scanning(lv_obj_t *s)
{
    lv_obj_t *col = kit_col(s, SP_4);
    lv_obj_set_width(col, LV_PCT(100));
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_top(col, SP_4, 0);

    /* the camera indicator: what vision says */
    const app_vision_t *v = app_vision();
    const bool on = v->connected && v->camera == APP_CAMERA_ON;
    lv_obj_t *ind = kit_row(col, SP_2);
    lv_obj_set_style_radius(ind, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ind, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(ind, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(ind, SP_4, 0);
    lv_obj_set_style_pad_ver(ind, SP_2, 0);
    P->cam_dot = lv_obj_create(ind);
    lv_obj_remove_style_all(P->cam_dot);
    lv_obj_set_size(P->cam_dot, 14, 14);
    lv_obj_set_style_radius(P->cam_dot, LV_RADIUS_CIRCLE, 0);
    const uint32_t dot = on ? C_CAMERA : v->camera == APP_CAMERA_ERROR || !v->connected ? C_DANGER : C_TEXT_3;
    lv_obj_set_style_bg_color(P->cam_dot, C(dot), 0);
    lv_obj_set_style_bg_opa(P->cam_dot, LV_OPA_COVER, 0);
    if (on) {
        lv_obj_set_style_shadow_color(P->cam_dot, C(C_CAMERA), 0);
        lv_obj_set_style_shadow_width(P->cam_dot, 16, 0);
        lv_obj_set_style_shadow_opa(P->cam_dot, LV_OPA_60, 0);
    }
    char ct[128];
    if (!v->connected) {
        snprintf(ct, sizeof(ct), "The camera service isn't running");
    } else if (v->camera == APP_CAMERA_ERROR) {
        snprintf(ct, sizeof(ct), "Camera error%s%s", v->msg[0] ? ": " : "", v->msg);
    } else if (on) {
        snprintf(ct, sizeof(ct), "Camera on \xc2\xb7 looking for a QR code");
    } else {
        snprintf(ct, sizeof(ct), "Starting the camera\xe2\x80\xa6");
    }
    P->cam_text = ui_text(ind, F.small_b, on ? C_TEXT : dot == C_DANGER ? C_DANGER : C_TEXT_2, ct, 0, 0);
    lv_obj_set_width(P->cam_text, LV_SIZE_CONTENT);
    lv_obj_set_style_max_width(P->cam_text, UI_CARD_W - 80, 0);

    /* the finder */
    lv_obj_t *f = lv_obj_create(col);
    lv_obj_remove_style_all(f);
    lv_obj_remove_flag(f, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(f, FINDER, FINDER);
    lv_obj_set_style_radius(f, 36, 0);
    lv_obj_set_style_bg_color(f, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(f, LV_OPA_COVER, 0);
    lv_obj_set_style_clip_corner(f, true, 0);
    lv_obj_set_style_margin_ver(f, SP_2, 0);
    lv_obj_t *q = kit_icon(f, fx()->icon_xl, ICONX_QR_CODE, C_SURFACE_3);
    lv_obj_set_style_transform_scale(q, 400, 0);
    lv_obj_update_layout(q);
    lv_obj_set_style_transform_pivot_x(q, lv_obj_get_width(q) / 2, 0);
    lv_obj_set_style_transform_pivot_y(q, lv_obj_get_height(q) / 2, 0);
    lv_obj_center(q);
    corner(f, LV_ALIGN_TOP_LEFT, LV_BORDER_SIDE_TOP | LV_BORDER_SIDE_LEFT);
    corner(f, LV_ALIGN_TOP_RIGHT, LV_BORDER_SIDE_TOP | LV_BORDER_SIDE_RIGHT);
    corner(f, LV_ALIGN_BOTTOM_LEFT, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_LEFT);
    corner(f, LV_ALIGN_BOTTOM_RIGHT, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_RIGHT);
    if (on) {
        P->scanline = lv_obj_create(f);
        lv_obj_remove_style_all(P->scanline);
        lv_obj_set_size(P->scanline, FINDER - 80, 4);
        lv_obj_set_x(P->scanline, 40);
        lv_obj_set_style_radius(P->scanline, 2, 0);
        lv_obj_set_style_bg_color(P->scanline, C(C_ACCENT), 0);
        lv_obj_set_style_bg_opa(P->scanline, LV_OPA_COVER, 0);
        lv_obj_set_style_shadow_color(P->scanline, C(C_ACCENT), 0);
        lv_obj_set_style_shadow_width(P->scanline, 24, 0);
        lv_obj_set_style_shadow_opa(P->scanline, LV_OPA_50, 0);
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, P->scanline);
        lv_anim_set_exec_cb(&a, scan_line_y);
        lv_anim_set_values(&a, 48, FINDER - 52);
        lv_anim_set_duration(&a, 1600);
        lv_anim_set_playback_duration(&a, 1600);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
        lv_anim_start(&a);
    }
    lv_obj_t *t = centered(col, F.title, C_TEXT, "Point the camera at the QR code", 2);
    lv_obj_set_style_text_line_space(t, 0, 0);
    centered(col, F.small, C_TEXT_2,
             "The one Odysseus shows under Pair a phone, from 20 to 40 cm away.", 3);
    if (P->flow.hint && P->flow.msg[0]) {
        hint_box(col, P->flow.msg, C_DANGER);
    }
    lv_obj_t *bc = kit_col(col, SP_3);
    lv_obj_set_width(bc, LV_PCT(100));
    lv_obj_set_style_margin_top(bc, SP_2, 0);
    kit_button(bc, KIT_QUIET, NULL, NULL, "Cancel", back_clicked, NULL);
}

static void build_typing(lv_obj_t *s)
{
    lv_obj_t *head = kit_col(s, SP_2);
    lv_obj_set_width(head, LV_PCT(100));
    lv_obj_set_style_pad_hor(head, SP_1, 0);
    ui_text(head, F.body, C_TEXT_2, "Type what Odysseus shows under Pair a phone.", UI_CARD_W - SP_2, 3);
    const app_net_t *n = app_net();
    char initial[160] = "https://";
    if (n->hub[0]) {
        snprintf(initial, sizeof(initial), "https://%s", n->hub);
    }
    P->addr = te_field(s, "ODYSSEUS ADDRESS", "https://\xe2\x80\xa6", P->typed ? P->addr_text : initial, 300);
    P->code = te_field(s, "PAIRING CODE", "8 to 100 letters and digits", P->typed ? P->code_text : "", 100);
    te_focus(P->code);
    if (P->flow.hint && P->flow.msg[0]) {
        hint_box(s, P->flow.msg, C_DANGER);
    }
    lv_obj_t *bc = kit_row(s, SP_3);
    lv_obj_set_width(bc, LV_PCT(100));
    lv_obj_t *c = kit_button(bc, KIT_QUIET, NULL, NULL, "Cancel", back_clicked, NULL);
    lv_obj_set_width(c, (UI_CARD_W - SP_3) * 2 / 5);
    lv_obj_t *p = kit_button(bc, KIT_PRIMARY, fx()->icon_s, ICONX_LINK, "Pair", submit_clicked, NULL);
    lv_obj_set_flex_grow(p, 1);
    lv_obj_set_width(p, 1);
    P->kb = te_keyboard(P->page, submit, NULL);
}

static void spin(void *var, int32_t v)
{
    lv_obj_set_style_transform_rotation(var, v, 0);
}

static void build_result(lv_obj_t *s)
{
    lv_obj_t *col = kit_col(s, SP_4);
    lv_obj_set_width(col, LV_PCT(100));
    lv_obj_set_style_pad_top(col, SP_12 * 2, 0);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    const pair_stage_t st = P->flow.stage;
    lv_obj_t *r;
    if (st == PAIR_WAITING) {
        r = ring(col, 168, C_ACCENT, F.icon_xl, ICON_LOADER);
        lv_obj_t *ic = lv_obj_get_child(r, 0);
        lv_obj_update_layout(r);
        lv_obj_set_style_transform_pivot_x(ic, lv_obj_get_width(ic) / 2, 0);
        lv_obj_set_style_transform_pivot_y(ic, lv_obj_get_height(ic) / 2, 0);
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, ic);
        lv_anim_set_exec_cb(&a, spin);
        lv_anim_set_values(&a, 0, 3600);
        lv_anim_set_duration(&a, 1400);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_start(&a);
    } else if (st == PAIR_OK) {
        r = ring(col, 168, C_ACCENT, F.icon_xl, ICON_CHECK);
    } else {
        r = ring(col, 168, C_DANGER, F.icon_xl, ICON_CIRCLE_ALERT);
    }
    lv_obj_set_style_margin_bottom(r, SP_4, 0);
    centered(col, F.title, C_TEXT,
             st == PAIR_WAITING ? "Pairing\xe2\x80\xa6" : st == PAIR_OK ? "Paired" : "That didn't work", 2);
    centered(col, F.body, C_TEXT_2,
             st == PAIR_WAITING ? "Odysseus is checking the code. This takes a few seconds."
                                : P->flow.msg,
             5);
    lv_obj_t *bc = kit_col(col, SP_3);
    lv_obj_set_width(bc, LV_PCT(100));
    lv_obj_set_style_margin_top(bc, SP_6, 0);
    if (st == PAIR_OK) {
        centered(col, F.small, C_TEXT_3, "The first sync starts now.", 2);
        kit_button(bc, KIT_PRIMARY, NULL, NULL, "Done", done_clicked, NULL);
    } else if (st == PAIR_FAILED) {
        kit_button(bc, KIT_PRIMARY, NULL, NULL, "Try again", back_clicked, NULL);
        kit_button(bc, KIT_QUIET, NULL, NULL, "Cancel", done_clicked, NULL);
    }
}

static void fade(void *var, int32_t v)
{
    lv_obj_set_style_opa(var, (lv_opa_t)v, 0);
}

static void do_rebuild(void *unused)
{
    (void)unused;
    s_rebuild_queued = false;
    if (!P) {
        return;
    }
    const pair_stage_t st = P->flow.stage;
    const bool same = P->built_once && st == P->built;
    if (P->addr && P->code) {
        snprintf(P->addr_text, sizeof(P->addr_text), "%s", te_text(P->addr));
        snprintf(P->code_text, sizeof(P->code_text), "%s", te_text(P->code));
        P->typed = true;
    }
    if (P->kb) {
        lv_obj_delete(P->kb);
        P->kb = NULL;
    }
    lv_obj_clean(P->scroll);
    P->addr = P->code = P->cam_dot = P->cam_text = P->scanline = NULL;
    lv_obj_set_style_pad_bottom(P->scroll, st == PAIR_TYPING ? 440 + SP_6 : SP_10, 0);
    switch (st) {
    case PAIR_CHOOSE: build_choose(P->scroll); break;
    case PAIR_SCANNING: build_scanning(P->scroll); break;
    case PAIR_TYPING: build_typing(P->scroll); break;
    case PAIR_WAITING:
    case PAIR_OK:
    case PAIR_FAILED: build_result(P->scroll); break;
    }
    if (!same) {
        lv_obj_scroll_to_y(P->scroll, 0, LV_ANIM_OFF);
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, P->scroll);
        lv_anim_set_exec_cb(&a, fade);
        lv_anim_set_values(&a, 0, 255);
        lv_anim_set_duration(&a, UI_ANIM_FAST);
        lv_obj_set_style_opa(P->scroll, 0, 0);
        lv_anim_start(&a);
    }
    P->built = st;
    P->built_once = true;
}

/* Rebuilds run after the event that asked for them: the content they delete may hold the
 * button that was just tapped. */
static void rebuild(void)
{
    if (!s_rebuild_queued) {
        s_rebuild_queued = true;
        lv_async_call(do_rebuild, NULL);
    }
}

/* ---- the detail ---- */

static void build(lv_obj_t *page, void *arg)
{
    P = arg;
    P->page = page;
    P->scroll = ui_page_scroller(page);
    app_add_listener(on_app, NULL);
    P->timer = lv_timer_create(tick, 500, NULL);
    do_rebuild(NULL);
}

static void destroy(void *arg)
{
    pair_ui_t *p = arg;
    if (p == P) {
        app_remove_listener(on_app, NULL);
        P = NULL;
        app_set_qr_scan(false);
    }
    if (p->timer) {
        lv_timer_delete(p->timer);
    }
    free(p);
}

static lv_obj_t *scroller(lv_obj_t *page)
{
    return lv_obj_get_child(page, 0);
}

static const ui_detail_t PAIRING = {
    .build = build,
    .destroy = destroy,
    .scroller = scroller,
};

void pairing_open(void)
{
    if (P) {
        return;
    }
    pair_ui_t *p = calloc(1, sizeof(*p));
    if (!p) {
        return;
    }
    pair_reset(&p->flow);
    ui_push("Pairing", &PAIRING, p);
}
