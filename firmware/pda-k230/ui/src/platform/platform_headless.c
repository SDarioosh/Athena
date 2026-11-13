/* The headless backend: LVGL draws into one full-size XRGB8888 buffer (direct mode, so it
 * always holds the whole frame), a virtual pointer stands in for the finger, and the
 * clock can be driven by hand so animations land exactly where the screenshot wants. */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "platform.h"

static struct {
    lv_display_t *disp;
    lv_indev_t *ptr;
    uint32_t *fb;
    int32_t w, h;
    bool manual;
    uint32_t now_ms;
    int32_t x, y;
    bool down;
} HL;

static uint32_t tick_cb(void)
{
    if (HL.manual) {
        return HL.now_ms;
    }
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    (void)a;
    (void)px;
    lv_display_flush_ready(d);
}

static void read_cb(lv_indev_t *in, lv_indev_data_t *data)
{
    (void)in;
    data->point.x = HL.x;
    data->point.y = HL.y;
    data->state = HL.down ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

lv_display_t *headless_create(int32_t w, int32_t h)
{
    lv_tick_set_cb(tick_cb);
    HL.w = w;
    HL.h = h;
    const size_t bytes = (size_t)w * (size_t)h * 4;
    HL.fb = aligned_alloc(64, (bytes + 63) / 64 * 64);
    if (!HL.fb) {
        return NULL;
    }
    HL.disp = lv_display_create(w, h);
    lv_display_set_color_format(HL.disp, LV_COLOR_FORMAT_XRGB8888);
    lv_display_set_buffers(HL.disp, HL.fb, NULL, (uint32_t)bytes, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(HL.disp, flush_cb);
    HL.ptr = lv_indev_create();
    lv_indev_set_type(HL.ptr, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(HL.ptr, read_cb);
    lv_indev_set_display(HL.ptr, HL.disp);
    return HL.disp;
}

void headless_manual_time(bool on)
{
    if (on && !HL.manual) {
        HL.now_ms = tick_cb();
    }
    HL.manual = on;
}

void headless_advance(uint32_t ms)
{
    for (uint32_t t = 0; t < ms; t += 5) {
        HL.now_ms += 5;
        lv_timer_handler();
    }
}

void headless_touch(int32_t x, int32_t y, bool down)
{
    HL.x = x;
    HL.y = y;
    HL.down = down;
}

void headless_tap(int32_t x, int32_t y)
{
    headless_touch(x, y, true);
    headless_advance(60);
    headless_touch(x, y, false);
    headless_advance(60);
}

void headless_drag(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint32_t ms, bool release)
{
    headless_touch(x0, y0, true);
    headless_advance(40);
    const uint32_t steps = ms / 10 ? ms / 10 : 1;
    for (uint32_t i = 1; i <= steps; i++) {
        headless_touch(x0 + (x1 - x0) * (int32_t)i / (int32_t)steps, y0 + (y1 - y0) * (int32_t)i / (int32_t)steps,
                       true);
        headless_advance(10);
    }
    if (release) {
        headless_touch(x1, y1, false);
        headless_advance(40);
    }
}

int headless_png(const char *path)
{
    lv_obj_invalidate(lv_display_get_screen_active(HL.disp));
    lv_refr_now(HL.disp);
    return png_write_xrgb(path, HL.fb, HL.w, HL.h, HL.w);
}
