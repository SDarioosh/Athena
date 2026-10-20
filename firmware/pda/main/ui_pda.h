/* The PDA screens (docs/t5-pda-plan.md §5), drawn from the feed with epdiy, driven by
 * touch gestures and the two buttons. Runs in its own task and reads its input from the
 * pda_app event queue. */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    UI_TODAY,
    UI_BRIEFING,
    UI_CALENDAR,
    UI_ACTIONS,
    UI_CONVERSATIONS,
    UI_STATUS,
    UI_CONV_DETAIL,
    UI_TRANSCRIPT,
    UI_EVENT,
    UI_TEMPLATES,
    UI_NEW_EVENT,
    UI_SCREEN_COUNT,
} ui_screen_t;

esp_err_t ui_pda_start(void);

/* Console: show a transcript page by ref (from the cache, or queue a fetch). */
void ui_pda_open(const char *ref);

/* For the console: name <-> screen. */
const char *ui_screen_name(ui_screen_t s);
int ui_screen_from_name(const char *name);

#ifdef __cplusplus
}
#endif
