#include "theme.h"

#include <stdio.h>
#include <stdlib.h>

/* the .ttf files in fonts/, embedded by CMake (cmake/embed_file.cmake) */
extern const unsigned char font_inter_regular[], font_inter_semibold[], font_inter_bold[], font_lucide[];
extern const unsigned long font_inter_regular_len, font_inter_semibold_len, font_inter_bold_len,
    font_lucide_len;

ui_fonts_t F;
lv_style_t ST_CARD, ST_PRESSED, ST_FOCUS;

#define CACHE_GLYPHS 256

static lv_font_t *ttf(const unsigned char *data, unsigned long len, int px)
{
    lv_font_t *f = lv_tiny_ttf_create_data_ex(data, len, px, LV_FONT_KERNING_NORMAL, CACHE_GLYPHS);
    if (!f) {
        fprintf(stderr, "athena-pda: font %d px failed to load\n", px);
        abort();
    }
    return f;
}

static lv_font_t *icon_font(int px)
{
    return ttf(font_lucide, font_lucide_len, px);
}

/* A text font whose missing glyphs (the ICON_* codepoints) come from Lucide. Lucide's
 * glyphs sit on a 1000-unit square: the same pixel size lines them up with the text. */
static lv_font_t *text_font(const unsigned char *data, unsigned long len, int px, lv_font_t *icons)
{
    lv_font_t *f = ttf(data, len, px);
    f->fallback = icons;
    return f;
}

void theme_init(lv_display_t *disp)
{
    lv_font_t *i22 = icon_font(FS_CAPTION), *i26 = icon_font(FS_SMALL), *i28 = icon_font(FS_LEAD),
              *i30 = icon_font(FS_BODY), *i44 = icon_font(FS_TITLE);
    F.caption = text_font(font_inter_semibold, font_inter_semibold_len, FS_CAPTION, i22);
    F.small = text_font(font_inter_regular, font_inter_regular_len, FS_SMALL, i26);
    F.small_b = text_font(font_inter_semibold, font_inter_semibold_len, FS_SMALL, i26);
    F.body = text_font(font_inter_regular, font_inter_regular_len, FS_BODY, i30);
    F.body_b = text_font(font_inter_semibold, font_inter_semibold_len, FS_BODY, i30);
    F.lead = text_font(font_inter_regular, font_inter_regular_len, FS_LEAD, i28);
    F.title = text_font(font_inter_bold, font_inter_bold_len, FS_TITLE, i44);
    F.display = text_font(font_inter_bold, font_inter_bold_len, FS_DISPLAY, icon_font(FS_DISPLAY));
    F.icon_s = icon_font(24);
    F.icon = icon_font(32);
    F.icon_l = icon_font(48);
    F.icon_xl = icon_font(FS_HERO);

    lv_theme_t *th = lv_theme_default_init(disp, C(C_ACCENT), C(C_TEXT_2), true, F.body);
    lv_display_set_theme(disp, th);

    lv_style_init(&ST_CARD);
    lv_style_set_bg_color(&ST_CARD, C(C_SURFACE));
    lv_style_set_bg_opa(&ST_CARD, LV_OPA_COVER);
    lv_style_set_radius(&ST_CARD, UI_CARD_RADIUS);
    lv_style_set_border_width(&ST_CARD, 0);
    lv_style_set_pad_all(&ST_CARD, UI_CARD_PAD);
    lv_style_set_pad_row(&ST_CARD, 0);
    lv_style_set_shadow_width(&ST_CARD, 0);

    lv_style_init(&ST_PRESSED);
    lv_style_set_bg_color(&ST_PRESSED, C(C_SURFACE_2));
    lv_style_set_bg_opa(&ST_PRESSED, LV_OPA_COVER);

    lv_style_init(&ST_FOCUS);
    lv_style_set_outline_color(&ST_FOCUS, C(C_ACCENT));
    lv_style_set_outline_width(&ST_FOCUS, 4);
    lv_style_set_outline_pad(&ST_FOCUS, 4);

    lv_obj_t *scr = lv_display_get_screen_active(disp);
    lv_obj_set_style_bg_color(scr, C(C_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(scr, C(C_TEXT), 0);
    lv_obj_set_style_text_font(scr, F.body, 0);
}
