#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#include "epdiy.h"

int host_fb_outside;

static void px(uint8_t *fb, int x, int y, uint8_t v)
{
    if (x < 0 || y < 0 || x >= HOST_FB_W || y >= HOST_FB_H) {
        host_fb_outside++;
        return;
    }
    fb[y * HOST_FB_W + x] = v;
}

/* epdiy's 8-bit colours keep the shade in the upper nibble */
static uint8_t shade(uint8_t color)
{
    return (uint8_t)((color & 0xF0) | (color >> 4));
}

EpdFontProperties epd_font_properties_default(void)
{
    return (EpdFontProperties){.fg_color = 0, .bg_color = 15, .fallback_glyph = 0, .flags = 0};
}

const EpdGlyph *epd_get_glyph(const EpdFont *font, uint32_t cp)
{
    for (uint32_t i = 0; i < font->interval_count; i++) {
        const EpdUnicodeInterval *iv = &font->intervals[i];
        if (cp >= iv->first && cp <= iv->last) {
            return &font->glyph[iv->offset + (cp - iv->first)];
        }
        if (cp < iv->first) {
            return NULL;
        }
    }
    return NULL;
}

static uint32_t next_cp(const char **s)
{
    const unsigned char *p = (const unsigned char *)*s;
    uint32_t cp = p[0];
    int n = 1;
    if (p[0] >= 0xF0) {
        cp = p[0] & 0x07;
        n = 4;
    } else if (p[0] >= 0xE0) {
        cp = p[0] & 0x0F;
        n = 3;
    } else if (p[0] >= 0xC0) {
        cp = p[0] & 0x1F;
        n = 2;
    }
    for (int i = 1; i < n; i++) {
        if (!p[i]) {
            *s += i;
            return 0xFFFD;
        }
        cp = (cp << 6) | (p[i] & 0x3F);
    }
    *s += n;
    return cp;
}

/* Glyph pixels with a non-zero value, blended from bg to fg (4 bits each). */
static void draw_glyph(const EpdFont *font, const EpdGlyph *g, int x, int y, uint8_t *fb,
                       const EpdFontProperties *p)
{
    const size_t bw = (size_t)(g->width / 2 + g->width % 2);
    const size_t size = bw * g->height;
    if (!size) {
        return;
    }
    uint8_t *bm = malloc(size);
    if (font->compressed) {
        uLongf out = size;
        if (uncompress(bm, &out, font->bitmap + g->data_offset, g->compressed_size) != Z_OK) {
            free(bm);
            return;
        }
    } else {
        memcpy(bm, font->bitmap + g->data_offset, size);
    }
    for (int yy = 0; yy < g->height; yy++) {
        for (int xx = 0; xx < g->width; xx++) {
            uint8_t v = bm[yy * bw + xx / 2];
            v = (xx & 1) ? v >> 4 : v & 0xF;
            if (!v) {
                continue;
            }
            const int c = p->bg_color + (p->fg_color - p->bg_color) * v / 15;
            px(fb, x + g->left + xx, y - g->top + yy, (uint8_t)(c * 17));
        }
    }
    free(bm);
}

int epd_write_string(const EpdFont *font, const char *s, int *cx, int *cy, uint8_t *fb, const EpdFontProperties *p)
{
    const int x0 = *cx;
    while (*s) {
        const uint32_t cp = next_cp(&s);
        if (cp == '\n') {
            *cx = x0;
            *cy += font->advance_y;
            continue;
        }
        const EpdGlyph *g = epd_get_glyph(font, cp);
        if (!g) {
            g = epd_get_glyph(font, p->fallback_glyph);
        }
        if (!g) {
            continue;
        }
        draw_glyph(font, g, *cx, *cy, fb, p);
        *cx += g->advance_x;
    }
    return 0;
}

void epd_draw_hline(int x, int y, int length, uint8_t color, uint8_t *fb)
{
    for (int i = 0; i < length; i++) {
        px(fb, x + i, y, shade(color));
    }
}

void epd_fill_rect(EpdRect r, uint8_t color, uint8_t *fb)
{
    for (int y = r.y; y < r.y + r.height; y++) {
        epd_draw_hline(r.x, y, r.width, color, fb);
    }
}

void epd_draw_rect(EpdRect r, uint8_t color, uint8_t *fb)
{
    epd_draw_hline(r.x, r.y, r.width, color, fb);
    epd_draw_hline(r.x, r.y + r.height - 1, r.width, color, fb);
    for (int y = r.y + 1; y < r.y + r.height - 1; y++) {
        px(fb, r.x, y, shade(color));
        px(fb, r.x + r.width - 1, y, shade(color));
    }
}

void epd_draw_pixel(int x, int y, uint8_t color, uint8_t *fb)
{
    px(fb, x, y, shade(color));
}

/* Bresenham */
void epd_draw_line(int x0, int y0, int x1, int y1, uint8_t color, uint8_t *fb)
{
    const int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    const int dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;
    for (;;) {
        px(fb, x0, y0, shade(color));
        if (x0 == x1 && y0 == y1) {
            break;
        }
        const int e2 = 2 * err;
        if (e2 >= dy) {
            err += dy;
            x0 += sx;
        }
        if (e2 <= dx) {
            err += dx;
            y0 += sy;
        }
    }
}

/* Pixels whose centre is within r + 0.5 of (x, y): filled, or the outline only. */
static void circle(int x, int y, int r, uint8_t color, uint8_t *fb, bool fill)
{
    const int out = (2 * r + 1) * (2 * r + 1), in = (2 * r - 1) * (2 * r - 1);
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            const int d = 4 * (dx * dx + dy * dy);
            if (d <= out && (fill || d > in)) {
                px(fb, x + dx, y + dy, shade(color));
            }
        }
    }
}

void epd_draw_circle(int x, int y, int r, uint8_t color, uint8_t *fb)
{
    circle(x, y, r, color, fb, false);
}

void epd_fill_circle(int x, int y, int r, uint8_t color, uint8_t *fb)
{
    circle(x, y, r, color, fb, true);
}
