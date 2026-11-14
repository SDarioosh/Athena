/* The on-screen keyboard and a text field for it (any screen: the new event's title, the
 * pairing code). One keyboard at a time; it slides up at the bottom of the field's layer
 * (a tab's content area, above the tab bar; a detail page; the sheet) and shrinks the
 * field's scroller to the space above it, so what is being typed stays in view.
 *
 *   lv_obj_t *f = ui_text_field(card, "Title", 200, UI_KB_TEXT);
 *   lv_obj_add_event_cb(f, on_text, LV_EVENT_VALUE_CHANGED, NULL); // every change
 *   lv_obj_add_event_cb(f, on_done, LV_EVENT_READY, NULL);         // "Done" was tapped
 *   lv_textarea_get_text(f);
 *
 * A tap on the field opens the keyboard; "Done" (or ui_keyboard_close()) closes it. It
 * closes itself when the field is deleted. The field is an lv_textarea: its text, cursor
 * and limit are LVGL's (lv_textarea_set_text, ...). Line breaks are never typed. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    UI_KB_TEXT,   /* letters (the first one capital), then 123 for digits and punctuation */
    UI_KB_NUMBER, /* a number pad */
    UI_KB_CODE,   /* both cases (shift, no automatic capital), digits and / : . - _: a
                   * pairing code or an address, where case matters; one row taller */
} ui_kb_mode_t;

/* A themed, full-width text field of up to max_chars characters (0: no limit) that wraps
 * and grows to 3 lines; placeholder shows while it is empty. */
lv_obj_t *ui_text_field(lv_obj_t *parent, const char *placeholder, uint32_t max_chars, ui_kb_mode_t mode);

/* Open the keyboard for field (a ui_text_field) now; a no-op if it is open for it. */
void ui_keyboard_open(lv_obj_t *field);
/* Close it (animated). */
void ui_keyboard_close(void);
bool ui_keyboard_is_open(void);
/* Its height when open in that mode (for layouts that plan for it). */
int32_t ui_keyboard_height(ui_kb_mode_t mode);

#ifdef __cplusplus
}
#endif
