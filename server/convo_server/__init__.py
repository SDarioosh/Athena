"""The old name of the ``athena`` package, kept as an alias.

Commands and imports written before the rename keep working: ``convo_server.db.store``
is the very module object ``athena.db.store`` (one copy, so module state and
monkeypatching are shared), and ``python -m convo_server.cli ...`` runs
``athena/cli.py``. Nothing in this repo imports ``convo_server``; it is here for the
owner's units, cron jobs and notes, and is removed once the deploy has moved to the new
names.

The submodules are found by a meta-path finder, not by files in this folder, so no
import system path can load a second copy of an ``athena`` module under this name.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from types import ModuleType

try:
    import athena as _athena
except ModuleNotFoundError as exc:
    if exc.name != "athena":
        raise
    # A venv installed before the rename and not reinstalled since knows only this
    # folder (an editable install maps just ``convo_server``); ``athena`` sits beside it.
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import athena as _athena

_OLD = __name__ + "."
_NEW = _athena.__name__ + "."
# Submodules come only from the finder below; the path finders find nothing here.
__path__: list[str] = []


def __getattr__(name: str) -> object:
    """``convo_server.X`` is ``athena.X`` for anything ``athena`` itself has."""
    try:
        return getattr(_athena, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


class _AliasLoader(importlib.abc.Loader):
    """Hands out the already imported ``athena`` module instead of a new one."""

    def __init__(self, target: str) -> None:
        self.target = target

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self.target)
        # The import system writes the alias spec onto the module next; exec_module
        # puts the real one back.
        self._spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        module.__spec__ = self._spec

    def get_code(self, fullname: str):  # for ``python -m convo_server.<name>``
        spec = importlib.util.find_spec(self.target)
        if spec is None or spec.loader is None:
            raise ImportError(f"no module named {self.target!r}")
        return spec.loader.get_code(self.target)

    def is_package(self, fullname: str) -> bool:
        spec = importlib.util.find_spec(self.target)
        return bool(spec and spec.submodule_search_locations is not None)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD):
            return None
        new = _NEW + fullname[len(_OLD):]
        real = importlib.util.find_spec(new)
        if real is None:
            return None
        spec = importlib.machinery.ModuleSpec(
            fullname, _AliasLoader(new), origin=real.origin,
            is_package=real.submodule_search_locations is not None)
        if real.submodule_search_locations is not None:
            spec.submodule_search_locations = []  # the finder resolves its children too
        spec.has_location = real.has_location
        return spec


# First in line, before the path finders and any editable-install finder that still
# maps the old name to this folder.
if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
