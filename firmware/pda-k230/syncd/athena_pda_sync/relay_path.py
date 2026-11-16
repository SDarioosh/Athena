"""Where `athena_relay` is imported from: the one seam between syncd and the PC relay code.

In order:

1. `ATHENA_RELAY_PATH`, a directory that holds the `athena_relay` package (on the device:
   where the image installs it, e.g. `/usr/lib/athena-pda`);
2. an `athena_relay` already importable (installed beside syncd in site-packages, or on
   `PYTHONPATH`);
3. the repository: `firmware/recorder/tools`, found relative to this file (development).

Only the BLE-free modules are used (`pda_*`, `uploader`, `drain`, `protocol`, `anchors`);
they need nothing but the standard library. `bleak` is never imported.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ENV = "ATHENA_RELAY_PATH"
REPO_TOOLS = Path(__file__).resolve().parents[3] / "recorder" / "tools"


def ensure() -> str:
    """Make `import athena_relay` work; return the directory it comes from."""
    env = os.environ.get(ENV, "").strip()
    if env:
        path = str(Path(env).expanduser())
        if not (Path(path) / "athena_relay" / "__init__.py").is_file():
            raise ImportError(f"{ENV}={env} has no athena_relay package")
        if path not in sys.path:
            sys.path.insert(0, path)
        return path
    spec = importlib.util.find_spec("athena_relay")
    if spec is not None and spec.origin:
        return str(Path(spec.origin).parent.parent)
    if (REPO_TOOLS / "athena_relay" / "__init__.py").is_file():
        sys.path.insert(0, str(REPO_TOOLS))
        return str(REPO_TOOLS)
    raise ImportError(f"athena_relay not found: set {ENV} to the directory that holds it")
