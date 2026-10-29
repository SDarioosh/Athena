"""The old package name ``convo_server`` is an alias of ``athena``: the same module
objects, ``python -m convo_server.cli`` still runs, and a venv installed before the
rename (it knows only the ``convo_server`` folder) still finds ``athena``."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent


def _run(*args: str, cwd: Path = SERVER) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize("name", ["store", "config", "mcp_server", "api", "cli", "pipeline"])
def test_old_name_gives_the_same_module(name: str) -> None:
    import importlib

    old = importlib.import_module(f"convo_server.{name}")
    new = importlib.import_module(f"athena.{name}")
    assert old is new
    assert sys.modules[f"convo_server.{name}"] is new
    # the real module keeps its own name and spec
    assert new.__name__ == f"athena.{name}"
    assert new.__spec__.name == f"athena.{name}"


def test_from_imports_and_attributes() -> None:
    from convo_server.mcp_server import INSTRUCTIONS

    import athena.mcp_server
    import convo_server
    from convo_server import store

    assert INSTRUCTIONS is athena.mcp_server.INSTRUCTIONS
    assert store is sys.modules["athena.store"]
    assert convo_server.mcp_server is athena.mcp_server
    assert convo_server.__doc__ and "alias" in convo_server.__doc__
    with pytest.raises(AttributeError):
        convo_server.no_such_thing  # noqa: B018
    with pytest.raises(ModuleNotFoundError):
        __import__("convo_server.no_such_module")


def test_monkeypatch_through_one_name_is_seen_through_the_other(monkeypatch) -> None:
    import convo_server.config

    import athena.config

    monkeypatch.setattr("convo_server.config.get_settings", lambda: "patched")
    assert athena.config.get_settings() == "patched"
    monkeypatch.setattr(athena.config, "get_settings", lambda: "again")
    assert convo_server.config.get_settings() == "again"


def test_old_name_first_in_a_fresh_interpreter() -> None:
    """Imported through the old name first, each module still loads once."""
    code = textwrap.dedent("""
        import sys
        import convo_server.store, convo_server.api
        import athena.store, athena.api
        assert convo_server.store is athena.store
        assert convo_server.api is athena.api
        assert athena.store.__spec__.name == "athena.store"
        assert not [m for m in sys.modules if m.startswith("convo_server.")
                    and sys.modules[m] is not sys.modules["athena." + m.split(".", 1)[1]]]
        print("ok")
    """)
    result = _run("-c", code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("package", ["convo_server", "athena"])
def test_python_m_cli_help(package: str) -> None:
    result = _run("-m", f"{package}.cli", "--help")
    assert result.returncode == 0, result.stderr
    assert "usage: convo-server" in result.stdout
    assert "serve" in result.stdout


def test_python_m_mcp_server_runs() -> None:
    # mcp_server has no __main__ block: `-m` imports it and exits 0, as before the rename
    result = _run("-m", "convo_server.mcp_server")
    assert result.returncode == 0, result.stderr


def test_venv_installed_before_the_rename(tmp_path: Path) -> None:
    """An editable install made before the rename maps only ``convo_server`` to its
    folder; ``athena`` is found beside it."""
    code = textwrap.dedent(f"""
        import importlib.machinery, os, sys
        server = {str(SERVER)!r}
        # forget how this venv finds athena: no editable finders, no server/ on the path
        sys.meta_path = [f for f in sys.meta_path
                         if "editable" not in str(getattr(f, "__module__", ""))]
        sys.path = [p for p in sys.path if p and os.path.abspath(p) != server]
        for name in [m for m in sys.modules if m.split(".")[0] in ("athena", "convo_server")]:
            del sys.modules[name]

        class OldEditable:  # what setuptools' finder of the old install does
            @classmethod
            def find_spec(cls, fullname, path=None, target=None):
                if fullname == "convo_server":
                    return importlib.machinery.PathFinder.find_spec(fullname, [server])
                return None
        sys.meta_path.append(OldEditable)

        try:
            import athena
        except ModuleNotFoundError:
            pass
        else:
            raise SystemExit("athena importable without the alias: the test proves nothing")
        import convo_server.store
        import athena.store
        assert convo_server.store is athena.store
        assert athena.__file__ == os.path.join(server, "athena", "__init__.py")
        print("ok")
    """)
    result = _run("-c", code, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
