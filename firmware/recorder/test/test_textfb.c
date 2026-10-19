#include "check.h"
#include "font5x7.h"
#include "textfb.h"

#include <string.h>

int main(void)
{
    /* Every character the status screen uses has a real glyph, not the fallback box. */
    const uint8_t *box = font5x7_glyph('\x01');
    const char *used = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ %-.:/";
    for (const char *p = used; *p; p++) {
        CHECK(font5x7_glyph(*p) != box);
    }
    CHECK(font5x7_glyph('a') == font5x7_glyph('A'));
    CHECK(font5x7_glyph('#') == box);
    CHECK(font5x7_glyph(' ')[0] == 0 && font5x7_glyph(' ')[6] == 0);

    /* Colour packing: red in RGB565 is 0xF800, sent high byte first. */
    CHECK_EQ(textfb_rgb(255, 0, 0), 0x00F8);
    CHECK_EQ(textfb_rgb(0, 255, 0), 0xE007);
    CHECK_EQ(textfb_rgb(0, 0, 255), 0x1F00);
    CHECK_EQ(textfb_rgb(0, 0, 0), 0x0000);
    CHECK_EQ(textfb_rgb(255, 255, 255), 0xFFFF);

    static uint16_t px[40 * 20];
    textfb_t fb = {px, 40, 20};
    textfb_clear(&fb, 0x1234);
    CHECK_EQ(px[0], 0x1234);
    CHECK_EQ(px[40 * 20 - 1], 0x1234);

    /* 'I' at scale 1: the top row is 01110, so x=1..3 set, x=0 and x=4 clear. */
    textfb_clear(&fb, 0);
    int next = textfb_draw(&fb, 0, 0, "I", 1, 0xFFFF);
    CHECK_EQ(next, 6);
    CHECK_EQ(px[0], 0);
    CHECK_EQ(px[1], 0xFFFF);
    CHECK_EQ(px[2], 0xFFFF);
    CHECK_EQ(px[3], 0xFFFF);
    CHECK_EQ(px[4], 0);
    CHECK_EQ(px[1 * 40 + 2], 0xFFFF); /* stem */
    CHECK_EQ(px[1 * 40 + 1], 0);

    /* Scale 2 doubles every pixel. */
    textfb_clear(&fb, 0);
    next = textfb_draw(&fb, 2, 2, "I", 2, 0xAAAA);
    CHECK_EQ(next, 2 + 12);
    CHECK_EQ(px[2 * 40 + 2], 0);
    CHECK_EQ(px[2 * 40 + 4], 0xAAAA);
    CHECK_EQ(px[3 * 40 + 4], 0xAAAA);
    CHECK_EQ(px[4 * 40 + 6], 0xAAAA); /* stem, row 1 doubled */

    /* Drawing past the edge must not write out of bounds (would crash under sanitizers). */
    textfb_draw(&fb, 35, 15, "ZZZZ", 3, 0xFFFF);
    textfb_draw(&fb, -7, -7, "Z", 3, 0xFFFF);

    CHECK_EQ(textfb_text_width("", 4), 0);
    CHECK_EQ(textfb_text_width("AB", 1), 11);
    CHECK_EQ(textfb_text_width("MUTED", 4), 5 * 24 - 4);

    return checks_done("test_textfb");
}
