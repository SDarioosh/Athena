#include "textfb.h"

#include "font5x7.h"

#include <string.h>

uint16_t textfb_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    const uint16_t v = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
    return (uint16_t)((v << 8) | (v >> 8));
}

void textfb_clear(textfb_t *fb, uint16_t color)
{
    const int n = fb->width * fb->height;
    for (int i = 0; i < n; i++) {
        fb->pixels[i] = color;
    }
}

static void fill_rect(textfb_t *fb, int x, int y, int w, int h, uint16_t color)
{
    for (int yy = y; yy < y + h; yy++) {
        if (yy < 0 || yy >= fb->height) {
            continue;
        }
        for (int xx = x; xx < x + w; xx++) {
            if (xx < 0 || xx >= fb->width) {
                continue;
            }
            fb->pixels[yy * fb->width + xx] = color;
        }
    }
}

int textfb_draw(textfb_t *fb, int x, int y, const char *text, int scale, uint16_t color)
{
    if (scale < 1) {
        scale = 1;
    }
    for (const char *p = text; *p; p++) {
        const uint8_t *rows = font5x7_glyph(*p);
        for (int r = 0; r < FONT5X7_HEIGHT; r++) {
            for (int c = 0; c < FONT5X7_WIDTH; c++) {
                if (rows[r] & (0x10 >> c)) {
                    fill_rect(fb, x + c * scale, y + r * scale, scale, scale, color);
                }
            }
        }
        x += (FONT5X7_WIDTH + 1) * scale;
    }
    return x;
}

int textfb_text_width(const char *text, int scale)
{
    const int n = (int)strlen(text);
    if (n == 0) {
        return 0;
    }
    return n * (FONT5X7_WIDTH + 1) * scale - scale;
}
