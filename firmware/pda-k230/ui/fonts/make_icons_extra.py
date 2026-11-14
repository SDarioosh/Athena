#!/usr/bin/env python3
"""Make fonts/lucide-extra.ttf: the Lucide icons (ISC, LICENSE-Lucide.txt) the Talks, Status,
pairing and ambient screens use beyond those in lucide.ttf, for icon-only labels
(src/ui/icons_extra.h, ICONX_*). A file of its own so lucide.ttf and the Inter files stay
as make_fonts.py made them; the codepoints are Lucide's own (lucide-static 1.x, the same
numbering as lucide.ttf), so the two can be merged into one font later without renaming.

    python3 -m venv /tmp/ft && /tmp/ft/bin/pip install fonttools
    npm pack lucide-static@1.72.0 && tar xzf lucide-static-1.72.0.tgz
    /tmp/ft/bin/python make_icons_extra.py package/font/lucide.ttf
"""

import json
import sys
from pathlib import Path

from fontTools import subset

HERE = Path(__file__).resolve().parent

# Lucide names; the C side names them in src/ui/icons_extra.h (ICONX_*)
ICONS = [
    "qr-code", "scan-qr-code", "keyboard", "link", "unlink", "trash-2", "power", "sun-dim",
    "timer", "app-window", "thumbs-up", "pointer", "arrow-left", "arrow-right", "arrow-up",
    "globe", "info", "signal", "grab", "circle-check", "circle-x", "moon", "shield-check",
    "network", "user-round",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    lucide = Path(sys.argv[1])
    cps = json.loads((lucide.parent / "codepoints.json").read_text())
    opts = subset.Options()
    opts.layout_features = []
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    font = subset.load_font(str(lucide), opts)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=[cps[n] for n in ICONS])
    sub.subset(font)
    out = HERE / "lucide-extra.ttf"
    font.save(str(out))
    print(f"{out.name}: {out.stat().st_size} bytes")
    for n in ICONS:
        c = cps[n]
        utf8 = "".join(f"\\x{b:02X}" for b in chr(c).encode())
        print(f'#define ICONX_{n.upper().replace("-", "_")} "{utf8}" /* {n} U+{c:04X} */')
    return 0


if __name__ == "__main__":
    sys.exit(main())
