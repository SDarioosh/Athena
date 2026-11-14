"""syncd tests: the package, athena_relay and the PC relay's test fakes on the path.

Imported first by every test module (not a conftest.py: the PC relay's tests import names
from their own `conftest`, and two of that name clash when both suites run together)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The fake hub listens on 127.0.0.1: allow loopback hubs in the tests (and in the e2e
# rig's syncd, which inherits the environment); test_syncd_addresses unsets it.
os.environ.setdefault("ATHENA_PDA_DEV_LOOPBACK_HUB", "1")

SYNCD = Path(__file__).resolve().parents[1]
RELAY_TOOLS = SYNCD.parents[1] / "recorder" / "tools"

for p in (SYNCD, RELAY_TOOLS, RELAY_TOOLS / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
