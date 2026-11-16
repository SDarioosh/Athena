/* Building blocks of the Talks, Status and pairing screens, beside widgets.h: buttons,
 * settings rows (a value, a switch, a choice of chips), a big state line, a confirm sheet,
 * a hold-to-confirm button and round initials. All from theme.h; every tap target is
 * ui_focusable() and at least UI_TOUCH_MIN high. */
#pragma once

#include <stdbool.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* An icon-only label (font: F.icon*, or fx()->icon* for ICONX_*). */
lv_obj_t *kit_icon(lv_obj_t *parent, const lv_font_t *font, const char *glyph, uint32_t color);

/* A transparent column / row container sized to its content. */
lv_obj_t *kit_col(lv_obj_t *parent, int32_t gap);
lv_obj_t *kit_row(lv_obj_t *parent, int32_t gap);

typedef enum {
    KIT_PRIMARY,   /* accent fill */
    KIT_SECONDARY, /* surface, accent text */
    KIT_QUIET,     /* surface, secondary text */
    KIT_DANGER,    /* surface, danger text */
} kit_kind_t;

/* A full-width button: an icon (icon font + glyph, both may be NULL) and a label. cb gets
 * LV_EVENT_CLICKED with ud. Returns the button; its label is child 1 (child 0 the icon). */
lv_obj_t *kit_button(lv_obj_t *parent, kit_kind_t kind, const lv_font_t *icon_font, const char *icon,
                     const char *text, lv_event_cb_t cb, void *ud);
void kit_button_set_text(lv_obj_t *btn, const char *text);
void kit_button_set_enabled(lv_obj_t *btn, bool on);

/* A card's inner block with the card's side padding (for paragraphs and buttons in a card). */
lv_obj_t *kit_card_block(lv_obj_t *card, int32_t gap);

/* A rule between rows, inset from the left like Today's. */
void kit_rule(lv_obj_t *card);

/* A settings row: an icon (optional, ICONX_ or ICON_ with its font), a label, and a value on
 * the right (cut with "…"). Returns the value label. */
lv_obj_t *kit_value_row(lv_obj_t *card, const lv_font_t *icon_font, const char *icon, const char *label,
                        const char *value);

/* A row with a title, a line under it (may be NULL) and a switch; a tap anywhere on the row
 * toggles it. cb: LV_EVENT_VALUE_CHANGED on the switch (lv_obj_has_state(sw, LV_STATE_CHECKED)). */
lv_obj_t *kit_switch_row(lv_obj_t *card, const char *title, const char *sub, bool on, lv_event_cb_t cb,
                         void *ud);

/* A title and a row of chips to pick one (labels[n]); cb gets LV_EVENT_CLICKED on a chip whose
 * user data is its index (lv_event_get_user_data is ud; the chip index is
 * kit_choice_index(lv_event_get_current_target(e))). */
lv_obj_t *kit_choice_row(lv_obj_t *card, const char *title, const char *const *labels, int n, int selected,
                         lv_event_cb_t cb, void *ud);
int kit_choice_index(lv_obj_t *chip);

/* A big state line at the top of a card: an icon in a round tint, a title and a line. */
typedef struct {
    lv_obj_t *box, *ring, *icon, *title, *sub;
} kit_state_t;
void kit_state_line(lv_obj_t *card, kit_state_t *out);
void kit_state_set(kit_state_t *s, const lv_font_t *icon_font, const char *icon, uint32_t color,
                   const char *title, const char *sub);

/* A sheet asking to confirm: a title, a line, the confirm button (danger: in the danger
 * colour) and Cancel. on_yes runs after the sheet closes. */
void kit_confirm(const char *title, const char *text, const lv_font_t *icon_font, const char *icon,
                 const char *yes, bool danger, void (*on_yes)(void));

/* A button that acts only when held for hold_ms: it fills from the left while held; let go
 * early and it empties (and says so in a toast). */
lv_obj_t *kit_hold_button(lv_obj_t *parent, const lv_font_t *icon_font, const char *icon, const char *text,
                          uint32_t hold_ms, void (*on_done)(void));

/* A round avatar with a person's initials (size px). */
lv_obj_t *kit_avatar(lv_obj_t *parent, const char *name, int32_t size);

#ifdef __cplusplus
}
#endif
