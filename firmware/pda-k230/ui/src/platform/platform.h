/* The display and input backends behind one small interface:
 *   headless  always built: a memory framebuffer (PNG dumps, a virtual finger); tests, the
 *             screenshot tool, and the default on a PC
 *   sdl       -DPDA_SDL=ON (needs SDL2): a window on a PC, the mouse as the finger
 *   device    -DPDA_DEVICE=ON (needs libdrm): DRM/KMS + evdev touch on the K230
 * main.c picks one with ATHENA_PDA_BACKEND (headless | sdl | device); the default is the
 * best one compiled in. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    PLATFORM_HEADLESS,
    PLATFORM_SDL,
    PLATFORM_DEVICE,
} platform_kind_t;

/* After lv_init(): the display (UI_W x UI_H) and its pointer. NULL if the backend can't
 * start (logged). */
lv_display_t *platform_create(platform_kind_t kind);
bool platform_available(platform_kind_t kind);

/* The panel's power and brightness (the device writes /sys/class/backlight; the others do
 * nothing). */
void platform_screen_power(bool on);
void platform_brightness(int percent);

/* ---- headless only ---- */
lv_display_t *headless_create(int32_t w, int32_t h);
/* A manual clock for LVGL (screenshots, tests): time moves only with headless_advance(),
 * which also runs LVGL's timers in 5 ms steps. */
void headless_manual_time(bool on);
void headless_advance(uint32_t ms);
/* The virtual finger: down or up at (x, y). */
void headless_touch(int32_t x, int32_t y, bool down);
/* A tap / a drag, run through LVGL's input with the manual clock. */
void headless_tap(int32_t x, int32_t y);
void headless_drag(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint32_t ms, bool release);
/* Redraw now and write the framebuffer as an RGB PNG. 0 or -1. */
int headless_png(const char *path);

/* PNG of an XRGB8888 buffer (stride in pixels). 0 or -1. */
int png_write_xrgb(const char *path, const uint32_t *px, int32_t w, int32_t h, int32_t stride);

#ifdef __cplusplus
}
#endif
