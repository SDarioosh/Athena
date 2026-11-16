/* The on-screen keyboard (ui_keyboard.h): LVGL's lv_keyboard with our key maps, look and
 * key handling, on the top layer at the bottom of the field's layer. */
#include "ui_keyboard.h"

#include <string.h>

#include "theme.h"
#include "ui.h"

#define KB_ROW_H 84
#define KB_PAD 8
#define KB_GAP 8
#define FIELD_LINES 3

/* key labels that do something else than type themselves */
#define K_SHIFT "\xe2\x86\x91"     /* ↑ */
#define K_BACK "\xe2\x86\x90"      /* ← */
#define K_SPACE "space"
#define K_DONE "Done"
#define K_123 "123"
#define K_ABC "abc"
#define K_SYM "#+="

#define CK (LV_BUTTONMATRIX_CTRL_CHECKED | LV_BUTTONMATRIX_CTRL_NO_REPEAT) /* a control key */
#define LK(w) (LV_BUTTONMATRIX_CTRL_POPOVER | (w))                          /* a letter key */

static const char *const MAP_LOWER[] = {
    "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "\n",
    "a", "s", "d", "f", "g", "h", "j", "k", "l", "\n",
    K_SHIFT, "z", "x", "c", "v", "b", "n", "m", K_BACK, "\n",
    K_123, ",", K_SPACE, ".", K_DONE, ""};
static const char *const MAP_UPPER[] = {
    "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "\n",
    "A", "S", "D", "F", "G", "H", "J", "K", "L", "\n",
    K_SHIFT, "Z", "X", "C", "V", "B", "N", "M", K_BACK, "\n",
    K_123, ",", K_SPACE, ".", K_DONE, ""};
static const lv_buttonmatrix_ctrl_t CTRL_TEXT[] = {
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    CK | 6, LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), CK | 6,
    CK | 6, LK(3), 15, LK(3), CK | LV_BUTTONMATRIX_CTRL_CLICK_TRIG | 7};

static const char *const MAP_SPECIAL[] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
    "-", "/", ":", ";", "(", ")", "\xe2\x82\xac", "&", "@", "\"", "\n",
    K_SYM, ".", ",", "?", "!", "'", "#", "%", K_BACK, "\n",
    K_ABC, "+", K_SPACE, "=", K_DONE, ""};
static const char *const MAP_SYMBOLS[] = {
    "[", "]", "{", "}", "#", "%", "^", "*", "+", "=", "\n",
    "_", "\\", "|", "~", "<", ">", "$", "\xc2\xa3", "\xc2\xb7", "\xe2\x80\xa6", "\n",
    K_123, "\xe2\x80\x93", "\xe2\x80\x99", "\xc2\xb0", "\xc3\x97", "\xc2\xa7", "`", "\xc2\xbf", K_BACK, "\n",
    K_ABC, ",", K_SPACE, ".", K_DONE, ""};
static const lv_buttonmatrix_ctrl_t CTRL_SPECIAL[] = {
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    CK | 6, LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), CK | 6,
    CK | 6, LK(3), 15, LK(3), CK | LV_BUTTONMATRIX_CTRL_CLICK_TRIG | 7};

static const char *const MAP_NUMBER[] = {
    "1", "2", "3", "\n",
    "4", "5", "6", "\n",
    "7", "8", "9", "\n",
    K_BACK, "0", K_DONE, ""};
static const lv_buttonmatrix_ctrl_t CTRL_NUMBER[] = {
    1, 1, 1,
    1, 1, 1,
    1, 1, 1,
    CK | 1, 1, CK | LV_BUTTONMATRIX_CTRL_CLICK_TRIG | 1};

/* codes and addresses: case matters, so no automatic capital; digits on top and the
 * characters of a URL or a pairing code at the bottom (one row more than the others) */
static const char *const MAP_CODE_LOWER[] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
    "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "\n",
    "a", "s", "d", "f", "g", "h", "j", "k", "l", "\n",
    K_SHIFT, "z", "x", "c", "v", "b", "n", "m", K_BACK, "\n",
    "/", ":", ".", "-", "_", K_DONE, ""};
static const char *const MAP_CODE_UPPER[] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
    "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "\n",
    "A", "S", "D", "F", "G", "H", "J", "K", "L", "\n",
    K_SHIFT, "Z", "X", "C", "V", "B", "N", "M", K_BACK, "\n",
    "/", ":", ".", "-", "_", K_DONE, ""};
static const lv_buttonmatrix_ctrl_t CTRL_CODE[] = {
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4),
    CK | 6, LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), LK(4), CK | 6,
    LK(4), LK(4), LK(4), LK(4), LK(4), CK | LV_BUTTONMATRIX_CTRL_CLICK_TRIG | 10};
#define CODE_LOWER LV_KEYBOARD_MODE_USER_2
#define CODE_UPPER LV_KEYBOARD_MODE_USER_3

static int32_t kb_height(ui_kb_mode_t mode)
{
    const int32_t rows = mode == UI_KB_CODE ? 5 : 4;
    return rows * KB_ROW_H + (rows - 1) * KB_GAP + 2 * KB_PAD + SP_2;
}

static struct {
    lv_obj_t *kb, *field, *scroller;
    int32_t scroller_h;     /* the scroller's height style before it was shrunk */
    bool shrunk;
    bool shift_once;        /* capitals for one letter */
    ui_kb_mode_t mode;
    lv_timer_t *watch;
} K;

/* ---- the field ---- */

static void field_clicked(lv_event_t *e)
{
    ui_keyboard_open(lv_event_get_current_target(e));
}

static ui_kb_mode_t field_mode(lv_obj_t *field)
{
    const uint32_t n = lv_obj_get_event_count(field);
    for (uint32_t i = 0; i < n; i++) {
        lv_event_dsc_t *d = lv_obj_get_event_dsc(field, i);
        if (lv_event_dsc_get_cb(d) == field_clicked) {
            return (ui_kb_mode_t)(intptr_t)lv_event_dsc_get_user_data(d);
        }
    }
    return UI_KB_TEXT;
}

static void close_now(void);

static void field_deleted(lv_event_t *e)
{
    if (lv_event_get_current_target(e) == K.field) {
        K.field = NULL; /* the scroller is restored unless it goes too (scroller_deleted) */
        close_now();
    }
}

lv_obj_t *ui_text_field(lv_obj_t *parent, const char *placeholder, uint32_t max_chars, ui_kb_mode_t mode)
{
    lv_obj_t *ta = lv_textarea_create(parent);
    lv_obj_remove_style_all(ta);
    lv_obj_set_width(ta, LV_PCT(100));
    lv_obj_set_height(ta, LV_SIZE_CONTENT);
    const int32_t lh = lv_font_get_line_height(F.body);
    lv_obj_set_style_min_height(ta, UI_TOUCH_MIN + SP_4, 0);
    lv_obj_set_style_max_height(ta, FIELD_LINES * lh + (FIELD_LINES - 1) * 4 + 2 * SP_5, 0);
    lv_obj_set_style_bg_color(ta, C(C_SURFACE_2), 0);
    lv_obj_set_style_bg_opa(ta, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(ta, 20, 0);
    lv_obj_set_style_border_width(ta, 2, 0);
    lv_obj_set_style_border_color(ta, C(C_SURFACE_3), 0);
    lv_obj_set_style_border_color(ta, C(C_ACCENT), LV_STATE_FOCUSED);
    lv_obj_set_style_pad_hor(ta, SP_5, 0);
    lv_obj_set_style_pad_ver(ta, SP_5, 0);
    lv_obj_set_style_text_font(ta, F.body, 0);
    lv_obj_set_style_text_color(ta, C(C_TEXT), 0);
    lv_obj_set_style_text_line_space(ta, 4, 0);
    lv_obj_set_style_anim_duration(ta, UI_ANIM, 0);
    lv_obj_set_style_text_color(ta, C(C_TEXT_3), LV_PART_TEXTAREA_PLACEHOLDER);
    /* the cursor: an accent bar */
    lv_obj_set_style_bg_opa(ta, LV_OPA_TRANSP, LV_PART_CURSOR);
    lv_obj_set_style_border_side(ta, LV_BORDER_SIDE_LEFT, LV_PART_CURSOR);
    lv_obj_set_style_border_width(ta, 0, LV_PART_CURSOR);
    lv_obj_set_style_border_width(ta, 3, LV_PART_CURSOR | LV_STATE_FOCUSED);
    lv_obj_set_style_border_color(ta, C(C_ACCENT), LV_PART_CURSOR);
    lv_obj_set_style_anim_duration(ta, 600, LV_PART_CURSOR);
    lv_obj_set_scrollbar_mode(ta, LV_SCROLLBAR_MODE_OFF);
    lv_textarea_set_placeholder_text(ta, placeholder ? placeholder : "");
    if (max_chars) {
        lv_textarea_set_max_length(ta, max_chars);
    }
    lv_obj_add_event_cb(ta, field_clicked, LV_EVENT_CLICKED, (void *)(intptr_t)mode);
    lv_obj_add_event_cb(ta, field_deleted, LV_EVENT_DELETE, NULL);
    ui_focusable(ta, false);
    return ta;
}

/* ---- the keyboard ---- */

static void set_mode(lv_keyboard_mode_t m)
{
    lv_keyboard_set_mode(K.kb, m);
}

/* the first letter of an empty text field is a capital; a one-time shift ends after a
 * letter (codes: no automatic capital, only the shift) */
static void auto_shift(void)
{
    if (K.mode == UI_KB_CODE && K.shift_once && lv_keyboard_get_mode(K.kb) == CODE_UPPER) {
        set_mode(CODE_LOWER);
        K.shift_once = false;
        return;
    }
    if (K.mode != UI_KB_TEXT || !K.field) {
        return;
    }
    const char *t = lv_textarea_get_text(K.field);
    const lv_keyboard_mode_t m = lv_keyboard_get_mode(K.kb);
    if (!t[0] && m == LV_KEYBOARD_MODE_TEXT_LOWER) {
        set_mode(LV_KEYBOARD_MODE_TEXT_UPPER);
        K.shift_once = true;
    } else if (t[0] && K.shift_once && m == LV_KEYBOARD_MODE_TEXT_UPPER) {
        set_mode(LV_KEYBOARD_MODE_TEXT_LOWER);
        K.shift_once = false;
    }
}

static void key(lv_event_t *e)
{
    lv_obj_t *kb = lv_event_get_current_target(e);
    const uint32_t id = lv_buttonmatrix_get_selected_button(kb);
    if (id == LV_BUTTONMATRIX_BUTTON_NONE || !K.field) {
        return;
    }
    const char *txt = lv_buttonmatrix_get_button_text(kb, id);
    if (!txt) {
        return;
    }
    const lv_keyboard_mode_t m = lv_keyboard_get_mode(kb);
    if (strcmp(txt, K_SHIFT) == 0 && K.mode == UI_KB_CODE) {
        K.shift_once = m == CODE_LOWER; /* one capital, then back */
        set_mode(m == CODE_LOWER ? CODE_UPPER : CODE_LOWER);
    } else if (strcmp(txt, K_SHIFT) == 0) {
        K.shift_once = m == LV_KEYBOARD_MODE_TEXT_LOWER;
        set_mode(m == LV_KEYBOARD_MODE_TEXT_LOWER ? LV_KEYBOARD_MODE_TEXT_UPPER : LV_KEYBOARD_MODE_TEXT_LOWER);
    } else if (strcmp(txt, K_123) == 0) {
        set_mode(LV_KEYBOARD_MODE_SPECIAL);
    } else if (strcmp(txt, K_SYM) == 0) {
        set_mode(LV_KEYBOARD_MODE_USER_1);
    } else if (strcmp(txt, K_ABC) == 0) {
        K.shift_once = false;
        set_mode(LV_KEYBOARD_MODE_TEXT_LOWER);
        auto_shift();
    } else if (strcmp(txt, K_DONE) == 0) {
        lv_obj_t *f = K.field;
        ui_keyboard_close();
        lv_obj_send_event(f, LV_EVENT_READY, NULL);
    } else if (strcmp(txt, K_BACK) == 0) {
        lv_textarea_delete_char(K.field);
        if (K.mode == UI_KB_TEXT) {
            auto_shift();
        }
    } else {
        lv_textarea_add_text(K.field, strcmp(txt, K_SPACE) == 0 ? " " : txt);
        auto_shift();
    }
}

/* Done in the accent, like the screens' primary buttons */
static void key_draw(lv_event_t *e)
{
    lv_draw_task_t *t = lv_event_get_draw_task(e);
    const lv_draw_dsc_base_t *base = lv_draw_task_get_draw_dsc(t);
    if (base->part != LV_PART_ITEMS) {
        return;
    }
    lv_obj_t *kb = lv_event_get_current_target(e);
    const char *txt = lv_buttonmatrix_get_button_text(kb, base->id1);
    if (!txt || strcmp(txt, K_DONE) != 0) {
        return;
    }
    lv_draw_fill_dsc_t *fill = lv_draw_task_get_fill_dsc(t);
    if (fill) {
        fill->color = C(C_ACCENT);
    }
    lv_draw_label_dsc_t *label = lv_draw_task_get_label_dsc(t);
    if (label) {
        label->color = C(C_ON_ACCENT);
    }
}

static void kb_deleted(lv_event_t *e)
{
    if (lv_event_get_current_target(e) == K.kb) {
        K.kb = NULL;
    }
}

/* the layer the field is on: its ancestor right under the screen */
static lv_obj_t *field_layer(lv_obj_t *field)
{
    lv_obj_t *o = field;
    while (lv_obj_get_parent(o) && lv_obj_get_parent(o) != lv_screen_active()) {
        o = lv_obj_get_parent(o);
    }
    return o;
}

static lv_obj_t *field_scroller(lv_obj_t *field)
{
    for (lv_obj_t *p = lv_obj_get_parent(field); p; p = lv_obj_get_parent(p)) {
        if (lv_obj_has_flag(p, LV_OBJ_FLAG_SCROLLABLE) && (lv_obj_get_scroll_dir(p) & LV_DIR_VER)) {
            return p;
        }
    }
    return NULL;
}

static void scroller_deleted(lv_event_t *e)
{
    if (lv_event_get_current_target(e) == K.scroller) {
        K.scroller = NULL;
        K.shrunk = false;
    }
}

static void restore_scroller(void)
{
    if (K.scroller && K.shrunk) {
        lv_obj_remove_event_cb(K.scroller, scroller_deleted);
        lv_obj_set_height(K.scroller, K.scroller_h);
    }
    K.shrunk = false;
    K.scroller = NULL;
}

static void slide_y(void *var, int32_t v)
{
    lv_obj_set_y(var, v);
}

static void slid_out(lv_anim_t *a)
{
    lv_obj_delete(a->var);
}

static void close_now(void)
{
    if (K.watch) {
        lv_timer_delete(K.watch);
        K.watch = NULL;
    }
    restore_scroller();
    if (K.kb) {
        lv_obj_t *kb = K.kb;
        K.kb = NULL;
        lv_anim_delete(kb, slide_y);
        lv_obj_delete(kb);
    }
    K.field = NULL;
}

/* the field's page went away or moved (a tab change, a pop, the sheet closing): close */
static void watch(lv_timer_t *t)
{
    (void)t;
    if (!K.field) {
        close_now();
        return;
    }
    lv_obj_t *layer = field_layer(K.field);
    bool shown = lv_obj_get_x(layer) == 0;
    for (lv_obj_t *p = K.field; p && shown; p = lv_obj_get_parent(p)) {
        shown = !lv_obj_has_flag(p, LV_OBJ_FLAG_HIDDEN);
    }
    if (!shown) {
        ui_keyboard_close();
    }
}

static void style_kb(lv_obj_t *kb)
{
    lv_obj_remove_style_all(kb);
    lv_obj_set_size(kb, UI_W, kb_height(K.mode));
    lv_obj_set_style_bg_color(kb, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(kb, LV_OPA_COVER, 0);
    lv_obj_set_style_border_side(kb, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_border_width(kb, 1, 0);
    lv_obj_set_style_border_color(kb, C(C_SURFACE_3), 0);
    lv_obj_set_style_pad_hor(kb, KB_PAD, 0);
    lv_obj_set_style_pad_top(kb, KB_PAD + SP_2, 0);
    lv_obj_set_style_pad_bottom(kb, KB_PAD, 0);
    lv_obj_set_style_pad_row(kb, KB_GAP, 0);
    lv_obj_set_style_pad_column(kb, 6, 0);
    lv_obj_set_style_text_font(kb, F.body, 0);
    /* keys */
    lv_obj_set_style_bg_color(kb, C(C_SURFACE_3), LV_PART_ITEMS);
    lv_obj_set_style_bg_opa(kb, LV_OPA_COVER, LV_PART_ITEMS);
    lv_obj_set_style_radius(kb, 14, LV_PART_ITEMS);
    lv_obj_set_style_text_color(kb, C(C_TEXT), LV_PART_ITEMS);
    lv_obj_set_style_text_font(kb, F.body, LV_PART_ITEMS);
    lv_obj_set_style_border_width(kb, 0, LV_PART_ITEMS);
    lv_obj_set_style_shadow_width(kb, 0, LV_PART_ITEMS);
    lv_obj_set_style_bg_color(kb, C(C_TEXT_3), LV_PART_ITEMS | LV_STATE_PRESSED);
    /* control keys (checked) a step darker, their labels smaller */
    lv_obj_set_style_bg_color(kb, C(C_SURFACE_2), LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(kb, C(C_TEXT), LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_text_font(kb, F.small_b, LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_bg_color(kb, C(C_SURFACE_3), LV_PART_ITEMS | LV_STATE_CHECKED | LV_STATE_PRESSED);
}

void ui_keyboard_open(lv_obj_t *field)
{
    if (!field) {
        return;
    }
    if (K.kb && K.field == field) {
        return;
    }
    if (K.kb) {
        close_now();
    }
    K.field = field;
    K.mode = field_mode(field);
    K.shift_once = false;

    lv_obj_t *layer = field_layer(field);
    lv_area_t la;
    lv_obj_get_coords(layer, &la);
    const int32_t top = la.y2 + 1 - kb_height(K.mode);

    lv_obj_t *kb = lv_keyboard_create(lv_layer_top());
    K.kb = kb;
    style_kb(kb);
    lv_obj_remove_event_cb(kb, lv_keyboard_def_event_cb);
    lv_obj_add_event_cb(kb, key, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(kb, kb_deleted, LV_EVENT_DELETE, NULL);
    lv_obj_add_flag(kb, LV_OBJ_FLAG_SEND_DRAW_TASK_EVENTS);
    lv_obj_add_event_cb(kb, key_draw, LV_EVENT_DRAW_TASK_ADDED, NULL);
    lv_keyboard_set_popovers(kb, true); /* a held key shows above the finger, types on release */
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_LOWER, MAP_LOWER, CTRL_TEXT);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_UPPER, MAP_UPPER, CTRL_TEXT);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_SPECIAL, MAP_SPECIAL, CTRL_SPECIAL);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_USER_1, MAP_SYMBOLS, CTRL_SPECIAL);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_NUMBER, MAP_NUMBER, CTRL_NUMBER);
    lv_keyboard_set_map(kb, CODE_LOWER, MAP_CODE_LOWER, CTRL_CODE);
    lv_keyboard_set_map(kb, CODE_UPPER, MAP_CODE_UPPER, CTRL_CODE);
    lv_keyboard_set_mode(kb, K.mode == UI_KB_NUMBER ? LV_KEYBOARD_MODE_NUMBER
                             : K.mode == UI_KB_CODE ? CODE_LOWER
                                                    : LV_KEYBOARD_MODE_TEXT_LOWER);
    lv_keyboard_set_textarea(kb, field);
    lv_textarea_set_cursor_pos(field, LV_TEXTAREA_CURSOR_LAST);
    auto_shift();
    lv_obj_align(kb, LV_ALIGN_TOP_LEFT, 0, top);

    /* the field's scroller ends where the keyboard begins */
    lv_obj_t *s = field_scroller(field);
    if (s) {
        lv_area_t sa;
        lv_obj_get_coords(s, &sa);
        const int32_t overlap = sa.y2 + 1 - top;
        if (overlap > 0) {
            K.scroller = s;
            K.scroller_h = lv_obj_get_style_height(s, LV_PART_MAIN);
            K.shrunk = true;
            lv_obj_add_event_cb(s, scroller_deleted, LV_EVENT_DELETE, NULL);
            lv_obj_set_height(s, lv_obj_get_height(s) - overlap);
            lv_obj_update_layout(s);
        }
        lv_obj_scroll_to_view_recursive(field, LV_ANIM_ON);
    }

    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, kb);
    lv_anim_set_exec_cb(&a, slide_y);
    lv_anim_set_values(&a, la.y2 + 1, top);
    lv_anim_set_duration(&a, UI_ANIM);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_obj_set_y(kb, la.y2 + 1);
    lv_anim_start(&a);

    K.watch = lv_timer_create(watch, 50, NULL);
}

void ui_keyboard_close_within(lv_obj_t *root)
{
    for (lv_obj_t *o = K.field; o && root; o = lv_obj_get_parent(o)) {
        if (o == root) {
            ui_keyboard_close(); /* the field is detached now: no key reaches it */
            return;
        }
    }
}

void ui_keyboard_close(void)
{
    if (K.watch) {
        lv_timer_delete(K.watch);
        K.watch = NULL;
    }
    restore_scroller();
    if (K.field && K.kb) {
        lv_keyboard_set_textarea(K.kb, NULL);
    }
    K.field = NULL;
    if (!K.kb) {
        return;
    }
    lv_obj_t *kb = K.kb;
    K.kb = NULL;
    lv_obj_remove_event_cb(kb, kb_deleted);
    lv_obj_remove_flag(kb, LV_OBJ_FLAG_CLICKABLE);
    lv_anim_delete(kb, slide_y);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, kb);
    lv_anim_set_exec_cb(&a, slide_y);
    lv_anim_set_values(&a, lv_obj_get_y(kb), lv_obj_get_y(kb) + lv_obj_get_height(kb));
    lv_anim_set_duration(&a, UI_ANIM_FAST + 50);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in);
    lv_anim_set_completed_cb(&a, slid_out);
    lv_anim_start(&a);
}

bool ui_keyboard_is_open(void)
{
    return K.kb != NULL;
}

int32_t ui_keyboard_height(ui_kb_mode_t mode)
{
    return kb_height(mode);
}
