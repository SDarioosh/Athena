/* LVGL configuration for athena-pda (LVGL 9.5-dev, commit 59dc7e4). Only what differs
 * from LVGL's defaults (src/lv_conf_internal.h) is set here. The backends are chosen by
 * CMake: PDA_DEVICE (DRM + evdev) and PDA_SDL (an SDL window on a PC). */
#ifndef LV_CONF_H
#define LV_CONF_H

/* XRGB8888: what the K230's DRM plane takes, and the headless framebuffer's format */
#define LV_COLOR_DEPTH 32

/* Linux: the C library's allocator and string functions */
#define LV_USE_STDLIB_MALLOC LV_STDLIB_CLIB
#define LV_USE_STDLIB_STRING LV_STDLIB_CLIB
#define LV_USE_STDLIB_SPRINTF LV_STDLIB_CLIB

#define LV_DEF_REFR_PERIOD 16 /* ~60 fps for animations */
#define LV_DPI_DEF 330        /* 4.1" at 568 x 1232 */
#define LV_USE_OS LV_OS_NONE  /* one thread: the UI loop polls the sockets */

#define LV_USE_DRAW_SW 1
#define LV_DRAW_SW_COMPLEX 1
#define LV_DRAW_SW_SHADOW_CACHE_SIZE 64
#define LV_DRAW_SW_CIRCLE_CACHE_SIZE 16
#define LV_DRAW_LAYER_SIMPLE_BUF_SIZE (64 * 1024)
#define LV_CACHE_DEF_SIZE (512 * 1024)

#define LV_USE_LOG 1
#define LV_LOG_LEVEL LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF 1

#define LV_USE_ASSERT_NULL 1
#define LV_USE_ASSERT_MALLOC 1

/* Fonts: Inter and Lucide through tiny_ttf (src/ui/theme.c); Montserrat 14 is only the
 * fallback LVGL needs for its default theme. */
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14
#define LV_USE_TINY_TTF 1
#define LV_TINY_TTF_FILE_SUPPORT 0
#define LV_TINY_TTF_CACHE_GLYPH_CNT 256
#define LV_TINY_TTF_CACHE_KERNING_CNT 512

/* The Status screen shows the Tailscale login link as a QR code (plan "Tailscale"). */
#define LV_USE_QRCODE 1

#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 1
#define LV_THEME_DEFAULT_GROW 0
#define LV_THEME_DEFAULT_TRANSITION_TIME 120

#define LV_BUILD_EXAMPLES 0
#define LV_BUILD_DEMOS 0

#ifdef PDA_DEVICE
#define LV_USE_LINUX_DRM 1
#define LV_USE_LINUX_DRM_GBM_BUFFERS 0
#define LV_LINUX_DRM_USE_EGL 0
#define LV_USE_EVDEV 1
#endif

#ifdef PDA_SDL
#define LV_USE_SDL 1
#define LV_SDL_INCLUDE_PATH <SDL2/SDL.h>
#define LV_SDL_RENDER_MODE LV_DISPLAY_RENDER_MODE_DIRECT
#define LV_SDL_BUF_COUNT 1
#define LV_SDL_ACCELERATED 0
#define LV_SDL_FULLSCREEN 0
#define LV_SDL_DIRECT_EXIT 1
#endif

#endif /* LV_CONF_H */
