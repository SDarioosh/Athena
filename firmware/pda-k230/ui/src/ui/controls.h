/* Controls for forms and actions (Brief, Calendar, the new-event flow): full-width buttons,
 * selectable tiles (chips, the day and hour pickers), the floating "+" button. Their look
 * comes from theme.h; each is a ui_focusable() tap target of at least UI_TOUCH_MIN. */
#pragma once

#include <stdbool.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UI_BUTTON_H 88
#define UI_FAB_D 112

typedef enum {
    UI_BUTTON_PRIMARY,   /* accent fill: the one main action of a page */
    UI_BUTTON_SECONDARY, /* a surface with accent text */
} ui_button_kind_t;

/* A full-width button: icon (may be NULL) and text, centred. Returns the button; its label
 * is child 0. */
lv_obj_t *ui_button(lv_obj_t *parent, const char *icon, const char *text, ui_button_kind_t kind,
                    lv_event_cb_t cb, void *user_data);
void ui_button_set_text(lv_obj_t *button, const char *icon, const char *text);
/* Disabled: dimmed, taps go to LV_EVENT_CLICKED still (so a page can say why). */
void ui_button_set_enabled(lv_obj_t *button, bool on);
bool ui_button_enabled(lv_obj_t *button);

/* A selectable tile: a surface, accent-filled when selected (ui_choice_set). Labels inside
 * that set no colour of their own follow it. text may be NULL (add children yourself; the
 * tile is a centred column). w/h: LV_SIZE_CONTENT or px (h at least UI_TOUCH_MIN). */
lv_obj_t *ui_choice(lv_obj_t *parent, const char *text, int32_t w, int32_t h, lv_event_cb_t cb,
                    void *user_data);
void ui_choice_set(lv_obj_t *tile, bool selected);

/* The round accent "+" button at the bottom right of page, over its content. */
lv_obj_t *ui_fab(lv_obj_t *page, lv_event_cb_t cb, void *user_data);

#ifdef __cplusplus
}
#endif
