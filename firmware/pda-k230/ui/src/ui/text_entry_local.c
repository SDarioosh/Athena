#include "text_entry_local.h"

#include <string.h>

#include "theme.h"
#include "ui.h"
#include "widgets.h"

#define KB_H 440
#define FIELD_H 84

/* Key maps for a web address and a pairing code: digits, letters, and . : / - _ ? = &.
 * "Del" deletes, "ABC"/"abc" switch case, "Done" submits. */
static const char *const MAP_LOWER[] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
    "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "\n",
    "a", "s", "d", "f", "g", "h", "j", "k", "l", "\n",
    "ABC", "z", "x", "c", "v", "b", "n", "m", "Del", "\n",
    "/", ":", ".", "-", "_", "Done", "",
};
static const char *const MAP_UPPER[] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "\n",
    "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "\n",
    "A", "S", "D", "F", "G", "H", "J", "K", "L", "\n",
    "abc", "Z", "X", "C", "V", "B", "N", "M", "Del", "\n",
    "/", ":", ".", "-", "_", "Done", "",
};
#define SPECIAL (LV_BUTTONMATRIX_CTRL_CHECKED | LV_BUTTONMATRIX_CTRL_NO_REPEAT)
static const lv_buttonmatrix_ctrl_t CTRL[] = {
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1,
    SPECIAL | 3, 2, 2, 2, 2, 2, 2, 2, LV_BUTTONMATRIX_CTRL_CHECKED | 3,
    2, 2, 2, 2, 2, SPECIAL | 5,
};

static struct {
    lv_obj_t *kb, *ta;
    void (*on_done)(void *);
    void *ud;
} TE;

static void kb_deleted(lv_event_t *e)
{
    (void)e;
    memset(&TE, 0, sizeof(TE));
}

static void field_style(lv_obj_t *ta, bool focused)
{
    lv_obj_set_style_border_color(ta, C(focused ? C_ACCENT : C_SURFACE_3), 0);
}

void te_focus(lv_obj_t *ta)
{
    if (TE.ta && TE.ta != ta) {
        field_style(TE.ta, false);
        lv_obj_remove_state(TE.ta, LV_STATE_FOCUSED);
    }
    TE.ta = ta;
    if (ta) {
        field_style(ta, true);
        lv_obj_add_state(ta, LV_STATE_FOCUSED); /* shows the cursor */
    }
}

static void field_clicked(lv_event_t *e)
{
    te_focus(lv_event_get_target(e));
}

static void field_deleted(lv_event_t *e)
{
    if (lv_event_get_target(e) == TE.ta) {
        TE.ta = NULL;
    }
}

lv_obj_t *te_field(lv_obj_t *parent, const char *caption, const char *placeholder, const char *initial,
                   int max_len)
{
    lv_obj_t *col = lv_obj_create(parent);
    lv_obj_remove_style_all(col);
    lv_obj_remove_flag(col, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(col, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(col, SP_2, 0);
    lv_obj_t *c = ui_text(col, F.caption, C_TEXT_2, caption, UI_CARD_W, 1);
    lv_obj_set_style_text_letter_space(c, 2, 0);
    lv_obj_set_style_pad_left(c, SP_1, 0);

    lv_obj_t *ta = lv_textarea_create(col);
    lv_obj_remove_style_all(ta);
    lv_textarea_set_one_line(ta, true);
    lv_textarea_set_max_length(ta, (uint32_t)max_len);
    lv_textarea_set_placeholder_text(ta, placeholder ? placeholder : "");
    lv_textarea_set_text(ta, initial ? initial : "");
    lv_obj_set_size(ta, LV_PCT(100), FIELD_H);
    lv_obj_set_style_radius(ta, 22, 0);
    lv_obj_set_style_bg_color(ta, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(ta, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(ta, 2, 0);
    lv_obj_set_style_pad_hor(ta, SP_5, 0);
    lv_obj_set_style_pad_top(ta, (FIELD_H - lv_font_get_line_height(F.body)) / 2 - 2, 0);
    lv_obj_set_style_text_font(ta, F.body, 0);
    lv_obj_set_style_text_color(ta, C(C_TEXT), 0);
    lv_obj_set_style_text_color(ta, C(C_TEXT_3), LV_PART_TEXTAREA_PLACEHOLDER);
    lv_obj_set_style_text_font(ta, F.body, LV_PART_TEXTAREA_PLACEHOLDER);
    /* the cursor: a 3 px accent bar */
    lv_obj_set_style_border_side(ta, LV_BORDER_SIDE_LEFT, LV_PART_CURSOR | LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(ta, 3, LV_PART_CURSOR | LV_STATE_FOCUSED);
    lv_obj_set_style_border_color(ta, C(C_ACCENT), LV_PART_CURSOR | LV_STATE_FOCUSED);
    lv_obj_set_style_anim_duration(ta, 500, LV_PART_CURSOR | LV_STATE_FOCUSED);
    field_style(ta, false);
    lv_obj_add_flag(ta, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(ta, field_clicked, LV_EVENT_CLICKED, NULL);
    lv_obj_add_event_cb(ta, field_deleted, LV_EVENT_DELETE, NULL);
    ui_focusable(ta, false);
    if (!TE.ta) {
        te_focus(ta);
    }
    return ta;
}

static void key(lv_event_t *e)
{
    lv_obj_t *kb = lv_event_get_current_target(e);
    const uint32_t id = lv_buttonmatrix_get_selected_button(kb);
    if (id == LV_BUTTONMATRIX_BUTTON_NONE) {
        return;
    }
    const char *t = lv_buttonmatrix_get_button_text(kb, id);
    if (!t) {
        return;
    }
    if (strcmp(t, "ABC") == 0) {
        lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_UPPER);
    } else if (strcmp(t, "abc") == 0) {
        lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_LOWER);
    } else if (strcmp(t, "Done") == 0) {
        if (TE.on_done) {
            TE.on_done(TE.ud);
        }
    } else if (TE.ta && strcmp(t, "Del") == 0) {
        lv_textarea_delete_char(TE.ta);
    } else if (TE.ta) {
        lv_textarea_add_text(TE.ta, t);
    }
}

/* the Done key in the accent (a buttonmatrix styles all keys alike otherwise) */
static void draw_task(lv_event_t *e)
{
    lv_draw_task_t *t = lv_event_get_draw_task(e);
    const lv_draw_dsc_base_t *base = lv_draw_task_get_draw_dsc(t);
    if (!base || base->part != LV_PART_ITEMS) {
        return;
    }
    lv_obj_t *kb = lv_event_get_current_target(e);
    const char *txt = lv_buttonmatrix_get_button_text(kb, base->id1);
    if (!txt || strcmp(txt, "Done") != 0) {
        return;
    }
    lv_draw_fill_dsc_t *fill = lv_draw_task_get_fill_dsc(t);
    if (fill) {
        fill->color = C(C_ACCENT);
    }
    lv_draw_label_dsc_t *label = lv_draw_task_get_label_dsc(t);
    if (label) {
        label->color = C(C_ON_ACCENT);
        label->font = F.body_b;
    }
}

lv_obj_t *te_keyboard(lv_obj_t *parent, void (*on_done)(void *ud), void *ud)
{
    lv_obj_t *kb = lv_keyboard_create(parent);
    lv_obj_remove_event_cb(kb, lv_keyboard_def_event_cb);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_LOWER, MAP_LOWER, CTRL);
    lv_keyboard_set_map(kb, LV_KEYBOARD_MODE_TEXT_UPPER, MAP_UPPER, CTRL);
    lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_LOWER);
    lv_obj_add_event_cb(kb, key, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(kb, kb_deleted, LV_EVENT_DELETE, NULL);
    lv_obj_add_flag(kb, LV_OBJ_FLAG_SEND_DRAW_TASK_EVENTS);
    lv_obj_add_event_cb(kb, draw_task, LV_EVENT_DRAW_TASK_ADDED, NULL);
    lv_obj_set_size(kb, UI_W, KB_H);
    lv_obj_align(kb, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_add_flag(kb, LV_OBJ_FLAG_FLOATING);
    /* the look: keys on the surface, the special keys a step lighter */
    lv_obj_set_style_bg_color(kb, C(C_SURFACE), 0);
    lv_obj_set_style_bg_opa(kb, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(kb, 0, 0);
    lv_obj_set_style_radius(kb, 0, 0);
    lv_obj_set_style_pad_all(kb, SP_2, 0);
    lv_obj_set_style_pad_bottom(kb, SP_4, 0);
    lv_obj_set_style_pad_row(kb, SP_2, 0);
    lv_obj_set_style_pad_column(kb, 6, 0);
    lv_obj_set_style_bg_color(kb, C(C_SURFACE_3), LV_PART_ITEMS);
    lv_obj_set_style_bg_opa(kb, LV_OPA_COVER, LV_PART_ITEMS);
    lv_obj_set_style_bg_color(kb, C(C_SURFACE_2), LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_bg_color(kb, C(C_TEXT_3), LV_PART_ITEMS | LV_STATE_PRESSED);
    lv_obj_set_style_radius(kb, 12, LV_PART_ITEMS);
    lv_obj_set_style_border_width(kb, 0, LV_PART_ITEMS);
    lv_obj_set_style_shadow_width(kb, 0, LV_PART_ITEMS);
    lv_obj_set_style_text_font(kb, F.body, LV_PART_ITEMS);
    lv_obj_set_style_text_color(kb, C(C_TEXT), LV_PART_ITEMS);
    lv_obj_set_style_text_color(kb, C(C_TEXT_2), LV_PART_ITEMS | LV_STATE_CHECKED);
    TE.kb = kb;
    TE.on_done = on_done;
    TE.ud = ud;
    return kb;
}

const char *te_text(lv_obj_t *ta)
{
    return ta ? lv_textarea_get_text(ta) : "";
}
