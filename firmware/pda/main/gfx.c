#include "gfx.h"

#include <stdio.h>
#include <string.h>

#include "fonts.h"

#define LINE_MAX_BYTES 400

static uint8_t *s_fb;

void gfx_begin(uint8_t *fb)
{
    s_fb = fb;
}

/* Next code point; advances *s. Invalid bytes read as U+FFFD. */
static uint32_t utf8_next(const char **s)
{
    const unsigned char *p = (const unsigned char *)*s;
    uint32_t cp;
    int n;
    if (p[0] < 0x80) {
        cp = p[0];
        n = 1;
    } else if ((p[0] & 0xE0) == 0xC0) {
        cp = p[0] & 0x1F;
        n = 2;
    } else if ((p[0] & 0xF0) == 0xE0) {
        cp = p[0] & 0x0F;
        n = 3;
    } else if ((p[0] & 0xF8) == 0xF0) {
        cp = p[0] & 0x07;
        n = 4;
    } else {
        *s += 1;
        return 0xFFFD;
    }
    for (int i = 1; i < n; i++) {
        if ((p[i] & 0xC0) != 0x80) {
            *s += i;
            return 0xFFFD;
        }
        cp = (cp << 6) | (p[i] & 0x3F);
    }
    *s += n;
    return cp;
}

const EpdFont *gfx_fallback(const EpdFont *f)
{
    if (f == &FONT_TEXT_B) {
        return &FONT_TEXT;
    }
    if (f == &FONT_BIG || f == &FONT_HUGE) {
        return &FONT_BIG_REGULAR;
    }
    return NULL;
}

/* The font that draws cp: f, else its fallback; NULL if neither has it. */
static const EpdFont *font_for(const EpdFont *f, uint32_t cp)
{
    if (epd_get_glyph(f, cp)) {
        return f;
    }
    const EpdFont *fb = gfx_fallback(f);
    return fb && epd_get_glyph(fb, cp) ? fb : NULL;
}

static int glyph_w(const EpdFont *f, uint32_t cp)
{
    const EpdFont *ff = font_for(f, cp);
    const EpdGlyph *g = ff ? epd_get_glyph(ff, cp) : epd_get_glyph(f, '?');
    return g ? g->advance_x : 0;
}

/* Characters the fonts lack, spelled with ones they have (plan §11 Q6: Latin-1 v1). Other
 * missing ones draw as '?', and emoji are dropped. */
static const char *subst(uint32_t cp)
{
    switch (cp) {
    case 0x20AC: return "EUR";
    case 0x2212: return "-";
    case 0x2248: return "~";
    case 0x2264: return "<=";
    case 0x2265: return ">=";
    default: return cp >= 0x1F000 ? "" : NULL;
    }
}

/* Copy s into out (cap bytes, NUL-terminated) with missing glyphs replaced. */
static const char *translit(const EpdFont *f, const char *s, char *out, size_t cap)
{
    size_t w = 0;
    const char *p = s;
    while (*p && w + 5 < cap) {
        const char *start = p;
        const uint32_t cp = utf8_next(&p);
        const char *rep = NULL;
        if (cp >= 0x80 && !font_for(f, cp)) {
            rep = subst(cp);
            if (!rep) {
                rep = "?";
            }
        }
        if (rep) {
            const size_t n = strlen(rep);
            if (w + n + 1 > cap) {
                break;
            }
            memcpy(out + w, rep, n);
            w += n;
        } else {
            memcpy(out + w, start, (size_t)(p - start));
            w += (size_t)(p - start);
        }
    }
    out[w] = 0;
    return out;
}

static int width_n(const EpdFont *f, const char *s, size_t n)
{
    const char *end = s + n;
    int w = 0;
    while (s < end && *s) {
        w += glyph_w(f, utf8_next(&s));
    }
    return w;
}

int gfx_text_width(const EpdFont *f, const char *s)
{
    static char buf[LINE_MAX_BYTES * 2];
    translit(f, s, buf, sizeof(buf));
    return width_n(f, buf, strlen(buf));
}

int gfx_line(const EpdFont *f)
{
    return f->advance_y;
}

/* Draw buf (already transliterated) in runs: characters f lacks come from its fallback. */
static void draw_runs(const EpdFont *f, int x, int y, const char *buf, uint8_t fg, uint8_t bg)
{
    EpdFontProperties p = epd_font_properties_default();
    p.fg_color = fg;
    p.bg_color = bg;
    p.fallback_glyph = '?';
    int cx = x;
    const char *q = buf;
    while (*q) {
        const char *start = q;
        const char *t = q;
        const EpdFont *rf = font_for(f, utf8_next(&t));
        if (!rf) {
            rf = f;
        }
        q = t;
        while (*q) {
            const char *u = q;
            const EpdFont *nf = font_for(f, utf8_next(&u));
            if ((nf ? nf : f) != rf) {
                break;
            }
            q = u;
        }
        char run[LINE_MAX_BYTES + 1];
        const size_t n = (size_t)(q - start);
        memcpy(run, start, n);
        run[n] = 0;
        int cy = y;
        epd_write_string(rf, run, &cx, &cy, s_fb, &p);
    }
}

int gfx_text(const EpdFont *f, int x, int y, const char *s, uint8_t color)
{
    if (!s || !*s) {
        return x;
    }
    /* epdiy treats '\n' as a line break; a single line has none. */
    char buf[LINE_MAX_BYTES + 1];
    translit(f, s, buf, sizeof(buf));
    const size_t n = strlen(buf);
    for (size_t i = 0; i < n; i++) {
        if (buf[i] == '\n' || buf[i] == '\r' || buf[i] == '\t') {
            buf[i] = ' ';
        }
    }
    draw_runs(f, x, y, buf, color & 0xF, 0xF);
    return x + width_n(f, buf, n);
}

int gfx_text_inv(const EpdFont *f, int x, int y, const char *s)
{
    if (!s || !*s) {
        return x;
    }
    char buf[LINE_MAX_BYTES + 1];
    translit(f, s, buf, sizeof(buf));
    draw_runs(f, x, y, buf, 0xF, 0x0);
    return x + width_n(f, buf, strlen(buf));
}

/* Bytes of s that fit in max_w (whole code points). */
static size_t fit_bytes(const EpdFont *f, const char *s, int max_w)
{
    const char *p = s;
    int w = 0;
    while (*p) {
        const char *q = p;
        const int gw = glyph_w(f, utf8_next(&q));
        if (w + gw > max_w) {
            break;
        }
        w += gw;
        p = q;
    }
    return (size_t)(p - s);
}

int gfx_text_fit(const EpdFont *f, int x, int y, int max_w, const char *s, uint8_t color)
{
    if (!s) {
        return x;
    }
    static char tl[LINE_MAX_BYTES * 2];
    s = translit(f, s, tl, sizeof(tl));
    if (gfx_text_width(f, s) <= max_w) {
        return gfx_text(f, x, y, s, color);
    }
    static const char ELL[] = "\xe2\x80\xa6";
    const int ew = gfx_text_width(f, ELL);
    char buf[LINE_MAX_BYTES + 4];
    size_t n = fit_bytes(f, s, max_w - ew);
    if (n > LINE_MAX_BYTES) {
        n = LINE_MAX_BYTES;
    }
    while (n > 0 && s[n - 1] == ' ') {
        n--;
    }
    memcpy(buf, s, n);
    memcpy(buf + n, ELL, sizeof(ELL));
    return gfx_text(f, x, y, buf, color);
}

void gfx_text_right(const EpdFont *f, int x_right, int y, const char *s, uint8_t color)
{
    gfx_text(f, x_right - gfx_text_width(f, s), y, s, color);
}

typedef struct {
    const EpdFont *f;
    int x, y, w, lh;
    uint8_t color;
    int skip, max, lines, drawn;
    int widths[8]; /* of the drawn lines, for gfx_wrap_widths() */
    char line[LINE_MAX_BYTES + 4];
    size_t ll;
} wrap_t;

static wrap_t s_wrap; /* UI task only; keeps 400+ bytes off the stack */

/* Emit the current line; more = text follows it (the last drawn line then ends in "…"). */
static void wrap_emit(wrap_t *st, bool more)
{
    st->line[st->ll] = 0;
    if (st->lines >= st->skip && st->drawn < st->max) {
        const int y = st->y + st->drawn * st->lh;
        if (st->drawn < 8) {
            st->widths[st->drawn] = width_n(st->f, st->line, st->ll);
        }
        if (st->drawn == st->max - 1 && more) {
            memcpy(st->line + st->ll, "…", 4); /* cut to fit by gfx_text_fit */
            gfx_text_fit(st->f, st->x, y, st->w, st->line, st->color);
        } else {
            gfx_text(st->f, st->x, y, st->line, st->color);
        }
        st->drawn++;
    }
    st->lines++;
    st->ll = 0;
}

int gfx_wrap(const EpdFont *f, int x, int y, int w, const char *s, uint8_t color, int skip_lines,
             int max_lines)
{
    if (!s) {
        return 0;
    }
    static char tl[4096]; /* UI task only */
    s = translit(f, s, tl, sizeof(tl));
    s_wrap = (wrap_t){.f = f, .x = x, .y = y, .w = w, .lh = gfx_line(f), .color = color,
                  .skip = skip_lines, .max = max_lines};
    const char *p = s;
    while (*p) {
        if (*p == '\n') {
            wrap_emit(&s_wrap, p[1] != 0);
            p++;
            continue;
        }
        if (*p == ' ') {
            p++;
            continue;
        }
        const char *we = p;
        while (*we && *we != ' ' && *we != '\n') {
            we++;
        }
        const size_t wl = (size_t)(we - p);
        const size_t need = s_wrap.ll + (s_wrap.ll ? 1 : 0) + wl;
        if (need <= LINE_MAX_BYTES) {
            char cand[LINE_MAX_BYTES + 1];
            memcpy(cand, s_wrap.line, s_wrap.ll);
            size_t cl = s_wrap.ll;
            if (s_wrap.ll) {
                cand[cl++] = ' ';
            }
            memcpy(cand + cl, p, wl);
            cl += wl;
            if (width_n(f, cand, cl) <= w) {
                memcpy(s_wrap.line, cand, cl);
                s_wrap.ll = cl;
                p = we;
                continue;
            }
        }
        if (s_wrap.ll) {
            wrap_emit(&s_wrap, true);
            continue; /* retry the word on an empty line */
        }
        /* a word wider than the line: break it */
        size_t n = fit_bytes(f, p, w);
        if (n == 0) {
            n = 1;
        }
        if (n > wl) {
            n = wl;
        }
        if (n > LINE_MAX_BYTES) {
            n = LINE_MAX_BYTES;
        }
        memcpy(s_wrap.line, p, n);
        s_wrap.ll = n;
        p += n;
        if (*p && *p != ' ' && *p != '\n') {
            wrap_emit(&s_wrap, true);
        }
    }
    if (s_wrap.ll) {
        wrap_emit(&s_wrap, false);
    }
    return s_wrap.lines;
}

int gfx_wrap_widths(int *out, int max)
{
    const int n = s_wrap.drawn < max ? s_wrap.drawn : max;
    for (int i = 0; i < n && i < 8; i++) {
        out[i] = s_wrap.widths[i];
    }
    return n;
}

void gfx_strike(const EpdFont *f, int x, int y, int w)
{
    const int sy = y - f->ascender / 3;
    epd_draw_hline(x, sy, w, 0x00, s_fb);
    epd_draw_hline(x, sy + 1, w, 0x00, s_fb);
}

void gfx_hline(int x, int y, int w, uint8_t color)
{
    epd_draw_hline(x, y, w, color, s_fb);
}

void gfx_fill(EpdRect r, uint8_t color)
{
    epd_fill_rect(r, color, s_fb);
}

void gfx_frame(EpdRect r, uint8_t color)
{
    epd_draw_rect(r, color, s_fb);
    epd_draw_rect((EpdRect){r.x + 1, r.y + 1, r.width - 2, r.height - 2}, color, s_fb);
}

void gfx_button(EpdRect r, const char *label, bool inverted)
{
    if (inverted) {
        gfx_fill(r, 0x00);
    } else {
        gfx_fill(r, 0xFF);
        gfx_frame(r, 0x00);
    }
    const EpdFont *f = &FONT_TEXT_B;
    const int tw = gfx_text_width(f, label);
    const int ty = r.y + (r.height + f->ascender + f->descender) / 2;
    if (inverted) {
        gfx_text_inv(f, r.x + (r.width - tw) / 2, ty, label);
    } else {
        gfx_text(f, r.x + (r.width - tw) / 2, ty, label, GFX_INK);
    }
}

/* A thick stroke: the line drawn at offsets 0..w-1 across it. */
static void stroke(int x0, int y0, int x1, int y1, int w, uint8_t color)
{
    for (int i = 0; i < w; i++) {
        epd_draw_line(x0, y0 + i, x1, y1 + i, color, s_fb);
    }
}

void gfx_check(int cx, int cy, int r, bool done)
{
    if (done) {
        epd_fill_circle(cx, cy, r, 0x00, s_fb);
        /* a white tick: short leg down to the left, long leg up to the right */
        const int ax = cx - r / 2, ay = cy, bx = cx - r / 8, by = cy + r * 3 / 8;
        const int ex = cx + r / 2 + 1, ey = cy - r * 3 / 8;
        const int w = r >= 24 ? r / 5 : 3;
        stroke(ax, ay - w / 2, bx, by - w / 2, w, 0xFF);
        stroke(bx, by - w / 2, ex, ey - w / 2, w, 0xFF);
    } else {
        epd_draw_circle(cx, cy, r, 0x00, s_fb);
        epd_draw_circle(cx, cy, r - 1, 0x00, s_fb);
    }
}

void gfx_dot(int cx, int cy, int r)
{
    epd_fill_circle(cx, cy, r, 0x00, s_fb);
}

void gfx_battery_icon(int x, int y, int percent)
{
    /* the body 26x14 and a tip on its right, its middle 7 px above the baseline y */
    const int bw = 26, bh = 14, top = y - 7 - bh / 2;
    gfx_fill((EpdRect){x + bw, top + 4, 3, bh - 8}, 0x00);
    gfx_frame((EpdRect){x, top, bw, bh}, 0x00);
    const int p = percent < 0 ? 0 : percent > 100 ? 100 : percent;
    const int fw = (bw - 6) * p / 100;
    if (fw > 0) {
        gfx_fill((EpdRect){x + 3, top + 3, fw, bh - 6}, 0x00);
    }
}

int gfx_battery(int x_right, int y, int percent, bool charging)
{
    const int x = x_right - GFX_BATTERY_W;
    gfx_battery_icon(x, y, percent);
    char t[16];
    snprintf(t, sizeof(t), "%s%d%%", charging ? "+" : "", percent < 0 ? 0 : percent > 100 ? 100 : percent);
    const int tw = gfx_text_width(&FONT_TEXT, t);
    gfx_text(&FONT_TEXT, x - 6 - tw, y, t, GFX_INK);
    return x - 6 - tw;
}
