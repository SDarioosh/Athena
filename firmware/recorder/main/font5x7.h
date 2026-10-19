/* Minimal 5x7 bitmap font: digits, upper-case letters and a few symbols. Pure C; host-tested.
 * Lower-case letters map to upper case; unknown characters draw as a hollow box. */
#pragma once

#include <stdint.h>

#define FONT5X7_WIDTH 5
#define FONT5X7_HEIGHT 7

/* Returns 7 rows; bit 4 is the left-most pixel of each row. */
const uint8_t *font5x7_glyph(char c);
