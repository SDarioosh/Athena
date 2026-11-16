/* Backend selection, the SDL window (PDA_SDL) and the K230 device (PDA_DEVICE). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "board.h"
#include "platform.h"
#include "theme.h"

#define LOG(...) fprintf(stderr, "athena-pda: " __VA_ARGS__)

bool platform_available(platform_kind_t kind)
{
    switch (kind) {
    case PLATFORM_HEADLESS: return true;
#ifdef PDA_SDL
    case PLATFORM_SDL: return true;
#endif
#ifdef PDA_DEVICE
    case PLATFORM_DEVICE: return true;
#endif
    default: return false;
    }
}

#ifdef PDA_SDL
static lv_display_t *sdl_create(void)
{
    lv_display_t *d = lv_sdl_window_create(UI_W, UI_H);
    if (!d) {
        return NULL;
    }
    /* 1232 px is taller than most PC screens: half size unless asked otherwise */
    const char *z = getenv("ATHENA_PDA_ZOOM");
    lv_sdl_window_set_zoom(d, z ? (float)atof(z) : 0.6f);
    lv_sdl_window_set_title(d, "athena-pda");
    lv_indev_t *m = lv_sdl_mouse_create();
    lv_indev_set_display(m, d);
    return d;
}
#endif

#ifdef PDA_DEVICE
static lv_display_t *device_create(void)
{
    const char *card = getenv("ATHENA_PDA_DRM");
    const char *touch = getenv("ATHENA_PDA_TOUCH");
    lv_display_t *d = lv_linux_drm_create();
    if (!d) {
        return NULL;
    }
    if (lv_linux_drm_set_file(d, card ? card : "/dev/dri/card0", -1) != LV_RESULT_OK) {
        LOG("DRM: can't open %s\n", card ? card : "/dev/dri/card0");
        return NULL;
    }
    const int32_t w = lv_display_get_horizontal_resolution(d), h = lv_display_get_vertical_resolution(d);
    LOG("DRM: %dx%d\n", (int)w, (int)h);
    if (w != UI_W || h != UI_H) {
        LOG("DRM: the UI is laid out for %dx%d portrait; this mode is %dx%d\n", UI_W, UI_H, (int)w, (int)h);
    }
    /* the touch screen: $ATHENA_PDA_TOUCH, else the input device named goodix_ts */
    char found[64] = "/dev/input/event0";
    if (!touch) {
        if (!board_find_touch(BOARD_INPUT_ROOT, found, sizeof(found))) {
            LOG("evdev: no goodix_ts input device; trying %s\n", found);
        }
        touch = found;
    }
    lv_indev_t *t = lv_evdev_create(LV_INDEV_TYPE_POINTER, touch);
    if (!t) {
        LOG("evdev: can't open %s (no touch)\n", touch);
    } else {
        LOG("touch: %s\n", touch);
        lv_indev_set_display(t, d);
    }
    return d;
}
#endif

lv_display_t *platform_create(platform_kind_t kind)
{
    switch (kind) {
    case PLATFORM_HEADLESS: return headless_create(UI_W, UI_H);
#ifdef PDA_SDL
    case PLATFORM_SDL: return sdl_create();
#endif
#ifdef PDA_DEVICE
    case PLATFORM_DEVICE: return device_create();
#endif
    default: LOG("backend %d isn't compiled in\n", (int)kind); return NULL;
    }
}

static int s_brightness = 80; /* the level the screen comes back on at */

/* Screen off is brightness 0 (what LilyGO's launcher does; BOARD.md "Screen off"). */
void platform_screen_power(bool on)
{
#ifdef PDA_DEVICE
    if (board_set_brightness(BOARD_BACKLIGHT_ROOT, on ? s_brightness : 0) != 0) {
        LOG("backlight: can't write %s\n", BOARD_BACKLIGHT_ROOT);
    }
#else
    (void)on;
#endif
}

void platform_brightness(int percent)
{
    s_brightness = percent < 1 ? 1 : percent > 100 ? 100 : percent;
#ifdef PDA_DEVICE
    board_set_brightness(BOARD_BACKLIGHT_ROOT, s_brightness);
#endif
}
