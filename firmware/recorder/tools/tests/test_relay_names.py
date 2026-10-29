"""The package's old name `barvis_relay` still imports the same modules as `athena_relay`,
and the state dir of a relay set up before the rename is used where it is."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

import athena_relay
import pytest
from athena_relay import anchors

TOOLS = Path(__file__).resolve().parents[1]
SUBMODULES = sorted(m.name for m in pkgutil.iter_modules(athena_relay.__path__))


def test_submodules_listed() -> None:
    assert {"anchors", "drain", "protocol", "uploader", "pda_sync"} <= set(SUBMODULES)


@pytest.mark.parametrize("name", SUBMODULES)
def test_old_name_is_same_module(name: str) -> None:
    old = importlib.import_module(f"barvis_relay.{name}")
    assert old is importlib.import_module(f"athena_relay.{name}")
    import barvis_relay

    assert getattr(barvis_relay, name) is old


def test_old_name_first_in_fresh_interpreter() -> None:
    code = (
        "import barvis_relay.uploader as u\n"
        "from barvis_relay.anchors import default_state_dir\n"
        "from barvis_relay import drain\n"
        "import athena_relay.uploader, athena_relay.anchors, athena_relay.drain\n"
        "assert u is athena_relay.uploader\n"
        "assert default_state_dir is athena_relay.anchors.default_state_dir\n"
        "assert drain is athena_relay.drain\n"
        "assert u.__name__ == 'athena_relay.uploader'\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=TOOLS, check=True)


@pytest.fixture
def state_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


def test_state_dir_new_when_neither_exists(state_base: Path) -> None:
    assert anchors.default_state_dir() == state_base / "athena-relay"
    assert list(state_base.iterdir()) == []  # nothing created


def test_state_dir_keeps_existing_old_dir(state_base: Path) -> None:
    old = state_base / "barvis-relay"
    old.mkdir()
    (old / "anchors.json").write_text('{"s": {"anchor_ms": 1, "seen_ms": 1}}')
    assert anchors.default_state_dir() == old
    # Used as is: nothing moved, copied or created.
    assert sorted(p.name for p in state_base.iterdir()) == ["barvis-relay"]
    assert [p.name for p in old.iterdir()] == ["anchors.json"]


def test_state_dir_new_wins_when_both_exist(state_base: Path) -> None:
    (state_base / "barvis-relay").mkdir()
    (state_base / "athena-relay").mkdir()
    assert anchors.default_state_dir() == state_base / "athena-relay"


def test_state_dir_ignores_old_name_that_is_a_file(state_base: Path) -> None:
    (state_base / "barvis-relay").write_text("")
    assert anchors.default_state_dir() == state_base / "athena-relay"
