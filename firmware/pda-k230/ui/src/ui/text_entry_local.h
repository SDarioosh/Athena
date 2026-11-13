/* A small local text entry for the pairing screen: labelled one-line fields
 * (lv_textarea) and one on-screen keyboard (lv_keyboard with our own key maps, since the
 * Inter fonts have no LVGL symbol glyphs). Deliberately tiny and self-contained: the
 * shared keyboard component replaces it later behind the same four calls. */
#pragma once

#include <stdbool.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* A field: a caption above a rounded one-line text area; returns the text area. A tap
 * on it gives it the keyboard (te_keyboard). */
lv_obj_t *te_field(lv_obj_t *parent, const char *caption, const char *placeholder, const char *initial,
                   int max_len);

/* The keyboard at the bottom of parent; on_done runs on its Done key (ud passed). It
 * types into the field that has it (te_focus); the first field made gets it. */
lv_obj_t *te_keyboard(lv_obj_t *parent, void (*on_done)(void *ud), void *ud);
void te_focus(lv_obj_t *ta);
const char *te_text(lv_obj_t *ta);

#ifdef __cplusplus
}
#endif
