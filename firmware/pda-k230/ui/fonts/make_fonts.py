#!/usr/bin/env python3
"""Make the UI's font files: Inter (SIL OFL 1.1) and Lucide icons (ISC), cut down to the
characters the PDA shows, as TrueType (glyf) files for LVGL's tiny_ttf (stb_truetype).

    python3 -m venv /tmp/ft && /tmp/ft/bin/pip install fonttools
    /tmp/ft/bin/python make_fonts.py <Inter-Regular.otf dir> <lucide.ttf>

Inter 4.0 OTF comes from rsms/inter (Debian/Ubuntu: fonts-inter, /usr/share/fonts/opentype/
inter); lucide.ttf from the npm package lucide-static (font/lucide.ttf). The cubic CFF
outlines of Inter are turned into quadratic glyf outlines (cu2qu), so tiny_ttf reads them
the same way as any TrueType font. The output goes next to this script.
"""

import sys
from pathlib import Path

from fontTools import subset
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

HERE = Path(__file__).resolve().parent

# Latin-1, the Latin Extended-A letters of Western European languages, general
# punctuation (dashes, quotes, bullet, ellipsis, ‹ ›), €, ™, arrows, check marks, stars,
# circles and the minus sign.
TEXT_RANGES = [
    (0x20, 0x7E), (0xA0, 0xFF), (0x131, 0x131), (0x152, 0x153), (0x160, 0x161),
    (0x178, 0x178), (0x17D, 0x17E), (0x2009, 0x200A), (0x2010, 0x2027), (0x202F, 0x202F),
    (0x2030, 0x2030), (0x2039, 0x203A), (0x20AC, 0x20AC), (0x2122, 0x2122),
    (0x2190, 0x2199), (0x21BB, 0x21BB), (0x2212, 0x2212), (0x25B2, 0x25BC), (0x25CB, 0x25CF),
    (0x2605, 0x2606), (0x2713, 0x2713), (0x2717, 0x2717),
]

# Lucide names -> their codepoints in lucide.ttf (font/codepoints.json); the C side names
# them in src/ui/theme.h (ICON_*).
ICONS = [
    "sun", "newspaper", "calendar-days", "list-checks", "messages-square", "activity",
    "check", "refresh-cw", "wifi", "wifi-off", "battery", "battery-low", "battery-medium",
    "battery-full", "battery-charging", "camera", "cloud-off", "hand", "star", "map-pin",
    "chevron-right", "chevron-left", "x", "arrow-down", "clock", "upload", "circle-alert",
    "sparkles", "undo-2", "inbox", "loader-circle",
]


def otf_to_ttf(font: TTFont) -> None:
    """Replace the CFF table by glyf/loca (quadratic curves), in place."""
    order = font.getGlyphOrder()
    gs = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(gs)
        gs[name].draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    for name in order:
        glyf.glyphs[name].recalcBounds(glyf)
    font["loca"] = newTable("loca")
    font["glyf"] = glyf
    del font["CFF "]
    if "VORG" in font:
        del font["VORG"]
    maxp = font["maxp"]
    maxp.tableVersion = 0x00010000
    for k in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs",
              "maxInstructionDefs", "maxStackElements", "maxSizeOfInstructions",
              "maxComponentElements"):
        setattr(maxp, k, 0)
    maxp.maxZones = 1
    for k in ("maxPoints", "maxContours", "maxCompositePoints", "maxCompositeContours",
              "maxComponentDepth"):
        setattr(maxp, k, 0)
    font.recalcBBoxes = True  # maxp's point counts and head's bounds on save
    font["head"].glyphDataFormat = 0
    font["post"].formatType = 2.0
    font["post"].extraNames = []
    font["post"].mapping = {}
    font.sfntVersion = "\x00\x01\x00\x00"


def cut(src: Path, dst: Path, unicodes, layout=("kern",)) -> None:
    opts = subset.Options()
    opts.layout_features = list(layout)
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    opts.desubroutinize = True
    font = subset.load_font(str(src), opts)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=unicodes)
    sub.subset(font)
    if "CFF " in font:
        otf_to_ttf(font)
    font.save(str(dst))
    print(f"{dst.name}: {dst.stat().st_size} bytes, {len(font.getGlyphOrder())} glyphs")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    inter = Path(sys.argv[1])
    lucide = Path(sys.argv[2])
    text = [cp for a, b in TEXT_RANGES for cp in range(a, b + 1)]
    for weight in ("Regular", "SemiBold", "Bold"):
        cut(inter / f"Inter-{weight}.otf", HERE / f"Inter-{weight}.ttf", text)
    import json

    cps = json.loads((lucide.parent / "codepoints.json").read_text())
    cut(lucide, HERE / "lucide.ttf", [cps[n] for n in ICONS], layout=())
    for n in ICONS:
        print(f"  {n}: U+{cps[n]:04X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
