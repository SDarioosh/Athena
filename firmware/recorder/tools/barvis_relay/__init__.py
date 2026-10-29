"""Old name of `athena_relay`, kept so imports and `python -m barvis_relay…` commands in
older notes and scripts keep working. Every submodule is the same module object as in
`athena_relay` (`barvis_relay.uploader is athena_relay.uploader`). To be removed in the
final step of docs/athena-rename-plan.md; new code imports `athena_relay`.
"""

import importlib
import pkgutil
import sys

import athena_relay


def _alias_submodules() -> None:
    for info in pkgutil.iter_modules(athena_relay.__path__):
        mod = importlib.import_module(f"athena_relay.{info.name}")
        sys.modules[f"{__name__}.{info.name}"] = mod
        globals()[info.name] = mod


_alias_submodules()
