#include "icons_extra.h"

#include <stdio.h>
#include <stdlib.h>

#include "theme.h"

extern const unsigned char font_lucide_extra[], font_inter_bold[];
extern const unsigned long font_lucide_extra_len, font_inter_bold_len;

static lv_font_t *ttf(const unsigned char *data, unsigned long len, int px)
{
    lv_font_t *f = lv_tiny_ttf_create_data_ex(data, len, px, LV_FONT_KERNING_NORMAL, 64);
    if (!f) {
        fprintf(stderr, "athena-pda: font %d px failed to load\n", px);
        abort();
    }
    return f;
}

const ui_fonts_extra_t *fx(void)
{
    static ui_fonts_extra_t X;
    if (!X.icon) {
        X.icon_s = ttf(font_lucide_extra, font_lucide_extra_len, 24);
        X.icon = ttf(font_lucide_extra, font_lucide_extra_len, 32);
        X.icon_l = ttf(font_lucide_extra, font_lucide_extra_len, 48);
        X.icon_xl = ttf(font_lucide_extra, font_lucide_extra_len, FS_HERO);
        X.clock = ttf(font_inter_bold, font_inter_bold_len, FS_CLOCK);
    }
    return &X;
}
