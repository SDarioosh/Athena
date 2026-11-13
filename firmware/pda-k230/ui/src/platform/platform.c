/* Backend selection, the SDL window (PDA_SDL) and the K230 device (PDA_DEVICE). */
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
/* The first backlight under /sys/class/backlight, or NULL. */
static const char *backlight_dir(void)
{
    static char path[300];
    if (path[0]) {
        return path;
    }
    DIR *d = opendir("/sys/class/backlight");
    if (!d) {
        return NULL;
    }
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] != '.') {
            snprintf(path, sizeof(path), "/sys/class/backlight/%s", e->d_name);
            break;
        }
    }
    closedir(d);
    return path[0] ? path : NULL;
}

static void write_attr(const char *dir, const char *attr, long v)
{
    char p[340];
    snprintf(p, sizeof(p), "%s/%s", dir, attr);
    FILE *f = fopen(p, "w");
    if (f) {
        fprintf(f, "%ld\n", v);
        fclose(f);
    }
}

static long read_attr(const char *dir, const char *attr)
{
    char p[340];
    snprintf(p, sizeof(p), "%s/%s", dir, attr);
    FILE *f = fopen(p, "r");
    long v = -1;
    if (f) {
        if (fscanf(f, "%ld", &v) != 1) {
            v = -1;
        }
        fclose(f);
    }
    return v;
}

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
    lv_indev_t *t = lv_evdev_create(LV_INDEV_TYPE_POINTER, touch ? touch : "/dev/input/event0");
    if (!t) {
        LOG("evdev: can't open %s (no touch)\n", touch ? touch : "/dev/input/event0");
    } else {
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

void platform_screen_power(bool on)
{
#ifdef PDA_DEVICE
    const char *bl = backlight_dir();
    if (bl) {
        write_attr(bl, "bl_power", on ? 0 : 4); /* FB_BLANK_UNBLANK / FB_BLANK_POWERDOWN */
    }
#else
    (void)on;
#endif
}

void platform_brightness(int percent)
{
#ifdef PDA_DEVICE
    const char *bl = backlight_dir();
    const long max = bl ? read_attr(bl, "max_brightness") : -1;
    if (max > 0) {
        write_attr(bl, "brightness", (max * percent + 50) / 100);
    }
#else
    (void)percent;
#endif
}
