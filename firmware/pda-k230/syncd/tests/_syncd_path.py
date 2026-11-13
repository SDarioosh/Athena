"""syncd tests: the package, athena_relay and the PC relay's test fakes on the path.

Imported first by every test module (not a conftest.py: the PC relay's tests import names
from their own `conftest`, and two of that name clash when both suites run together)."""

from __future__ import annotations

import sys
from pathlib import Path

SYNCD = Path(__file__).resolve().parents[1]
RELAY_TOOLS = SYNCD.parents[1] / "recorder" / "tools"

for p in (SYNCD, RELAY_TOOLS, RELAY_TOOLS / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
