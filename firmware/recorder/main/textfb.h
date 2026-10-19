/* Text rendering into an RGB565 band buffer. Pure C; host-tested.
 * The board pushes the band to the panel; this file knows nothing about displays. */
#pragma once

#include <stdint.h>

typedef struct {
    uint16_t *pixels; /* width * height RGB565 values, row-major, already in panel byte order */
    int width;
    int height;
} textfb_t;

/* Fill the whole band with one colour. */
void textfb_clear(textfb_t *fb, uint16_t color);

/* Draw `text` with the 5x7 font scaled by `scale` (pixels per font pixel), top-left at
 * (x, y) in the band. Glyph pitch is 6 * scale. Pixels outside the band are dropped.
 * Returns the x position after the last glyph. */
int textfb_draw(textfb_t *fb, int x, int y, const char *text, int scale, uint16_t color);

/* Width in pixels of `text` at `scale`. */
int textfb_text_width(const char *text, int scale);

/* RGB888 -> RGB565 with the byte order the panel expects over SPI (big-endian). */
uint16_t textfb_rgb(uint8_t r, uint8_t g, uint8_t b);
