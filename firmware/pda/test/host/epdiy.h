/* Host stand-in for the parts of epdiy (vroland/epdiy 2.x) that gfx.c and ui_today.c use,
 * so the host tests can render screens into a plain 8-bit portrait image. Only the types
 * and calls the PDA uses; written from epdiy's public API, no epdiy code. The font headers
 * in main/fonts/ (epdiy's compressed 4-bit glyph format) compile against it unchanged. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    int x, y, width, height;
} EpdRect;

typedef struct {
    uint16_t width, height, advance_x;
    int16_t left, top;
    uint32_t compressed_size, data_offset;
} EpdGlyph;

typedef struct {
    uint32_t first, last, offset;
} EpdUnicodeInterval;

typedef struct {
    const uint8_t *bitmap;
    const EpdGlyph *glyph;
    const EpdUnicodeInterval *intervals;
    uint32_t interval_count;
    bool compressed;
    uint16_t advance_y;
    int ascender, descender;
} EpdFont;

typedef struct {
    uint8_t fg_color, bg_color; /* 4-bit */
    uint32_t fallback_glyph;
    int flags;
} EpdFontProperties;

EpdFontProperties epd_font_properties_default(void);
const EpdGlyph *epd_get_glyph(const EpdFont *font, uint32_t code_point);
int epd_write_string(const EpdFont *font, const char *string, int *cursor_x, int *cursor_y, uint8_t *framebuffer,
                     const EpdFontProperties *properties);
void epd_draw_hline(int x, int y, int length, uint8_t color, uint8_t *framebuffer);
void epd_fill_rect(EpdRect rect, uint8_t color, uint8_t *framebuffer);
void epd_draw_rect(EpdRect rect, uint8_t color, uint8_t *framebuffer);
void epd_draw_pixel(int x, int y, uint8_t color, uint8_t *framebuffer);
void epd_draw_line(int x0, int y0, int x1, int y1, uint8_t color, uint8_t *framebuffer);
void epd_draw_circle(int x, int y, int r, uint8_t color, uint8_t *framebuffer);
void epd_fill_circle(int x, int y, int r, uint8_t color, uint8_t *framebuffer);

/* Host only: the image is HOST_FB_W x HOST_FB_H bytes, one per pixel (0 black .. 255
 * white), portrait as the UI draws it. Pixels drawn outside it are counted, not drawn. */
#define HOST_FB_W 540
#define HOST_FB_H 960
extern int host_fb_outside;
