# Fonts

`firasans_12.h` and `firasans_20.h` are Fira Sans Regular at 12 and 20 pt, converted to
epdiy's compressed 4-bit glyph format by epdiy's `scripts/fontconvert.py`. They are copied
unchanged from epdiy's `examples/demo/main/` (component `vroland/epdiy` 2.1.3).

Coverage: ASCII, Latin-1 (U+00A0–U+00FF), general punctuation (U+2010–U+205F: dashes,
curly quotes, ellipsis, bullet), arrows, a few symbol blocks. That is the plan's v1 set
(docs/t5-pda-plan.md §2, §11 Q6). Other characters draw as the fallback glyph.

`firasans_sb_12.h`, `firasans_sb_20.h` and `firasans_sb_32.h` are Fira Sans SemiBold
(`FiraSans-SemiBold.ttf` from google/fonts, `ofl/firasans`) at 12, 20 and 32 pt, made with
epdiy 2.1.3's `scripts/fontconvert.py --compress --string <characters>` (freetype-py).
The string lists only characters the font has: with a code point range that holds missing
characters, the script writes ranges longer than its glyph table (reads past its end).
12 and 20 pt: ASCII, Latin-1 and the general punctuation the font has (228 glyphs);
32 pt: ASCII and · – — … (99 glyphs). Arrows, ✓, ★ and ▲ aren't in SemiBold: `gfx.c`
draws them from the Regular font of the nearest size (`gfx_fallback()`).

Fira Sans: digitized data copyright (c) 2012-2015, The Mozilla Foundation and Telefonica
S.A., under the SIL Open Font License 1.1 (`OFL-FiraSans.txt`).
