"""Where settings and the database file come from: the old CONVO_* names and an existing
convo.db keep working."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena import config
from athena.config import Settings
from athena.db.store import Store

TOKEN = "t" * 32


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path) -> None:
    """No CONVO_/ATHENA_ variable from the machine running the tests, and a cwd without
    a .env."""
    for name in list(os.environ):
        if name.upper().startswith(("CONVO_", "ATHENA_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def settings(env_file: Path | None = None, **kw) -> Settings:
    return Settings(_env_file=env_file, **kw)  # type: ignore[call-arg]


# (environment, .env lines) -> expected port; every row uses the old names only
OLD_NAMES = [
    ({}, [], 8000),
    ({"CONVO_PORT": "8100"}, [], 8100),
    ({}, ["CONVO_PORT=8200"], 8200),
    ({"CONVO_PORT": "8100"}, ["CONVO_PORT=8200"], 8100),  # environment beats .env
    ({"convo_port": "8300"}, [], 8300),  # any case
]


@pytest.mark.parametrize(("env", "lines", "port"), OLD_NAMES)
def test_old_names_resolve(env, lines, port, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CONVO_INGEST_TOKEN", TOKEN)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    dotenv = tmp_path / "test.env"
    dotenv.write_text("\n".join(lines) + "\n")
    assert settings(dotenv).port == port


def test_ingest_token_required(tmp_path) -> None:
    with pytest.raises(ValidationError) as e:
        settings(data_dir=tmp_path)
    assert "ingest_token" in str(e.value)


def test_ingest_token_old_name_in_env_or_dotenv(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / "test.env"
    dotenv.write_text(f"CONVO_INGEST_TOKEN={TOKEN}\n")
    assert settings(dotenv).ingest_token == TOKEN
    monkeypatch.setenv("CONVO_INGEST_TOKEN", "e" * 20)
    assert settings().ingest_token == "e" * 20


def test_existing_convo_db_is_used(tmp_path) -> None:
    (tmp_path / "convo.db").write_bytes(b"")
    s = settings(ingest_token=TOKEN, data_dir=tmp_path)
    assert s.db_path == tmp_path / "convo.db"
    assert not (tmp_path / "athena.db").exists()


def test_data_dir_from_old_name(monkeypatch, tmp_path) -> None:
    (tmp_path / "convo.db").write_bytes(b"")
    monkeypatch.setenv("CONVO_DATA_DIR", str(tmp_path))
    assert settings(ingest_token=TOKEN).db_path == tmp_path / "convo.db"


# ------------------------------------------------------------------ ATHENA_* names
@pytest.fixture
def warned(monkeypatch) -> set[str]:
    """A fresh 'already warned' set, so each test sees its own warnings."""
    fresh: set[str] = set()
    monkeypatch.setattr(config, "_warned", fresh)
    return fresh


# (environment, .env lines) -> expected port, with the new names in play
NEW_NAMES = [
    ({"ATHENA_PORT": "8100"}, [], 8100),
    ({}, ["ATHENA_PORT=8200"], 8200),
    ({"athena_port": "8300"}, [], 8300),
    ({"ATHENA_PORT": "8100", "CONVO_PORT": "8101"}, [], 8100),  # new name wins
    ({}, ["CONVO_PORT=8201", "ATHENA_PORT=8200"], 8200),  # in .env too, in any order
    ({}, ["ATHENA_PORT=8200", "CONVO_PORT=8201"], 8200),
    ({"CONVO_PORT": "8101"}, ["ATHENA_PORT=8200"], 8101),  # environment beats .env
    ({"ATHENA_PORT": "8100"}, ["CONVO_PORT=8201"], 8100),
    ({"ATHENA_PORT": "8100"}, ["ATHENA_PORT=8200"], 8100),
]


@pytest.mark.parametrize(("env", "lines", "port"), NEW_NAMES)
def test_new_names_resolve(env, lines, port, monkeypatch, tmp_path, warned) -> None:
    monkeypatch.setenv("ATHENA_INGEST_TOKEN", TOKEN)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    dotenv = tmp_path / "test.env"
    dotenv.write_text("\n".join(lines) + "\n")
    assert settings(dotenv).port == port


def test_arguments_beat_both_names(monkeypatch, warned) -> None:
    monkeypatch.setenv("ATHENA_PORT", "8100")
    monkeypatch.setenv("CONVO_PORT", "8101")
    assert settings(ingest_token=TOKEN, port=9000).port == 9000


def test_every_setting_has_both_names(monkeypatch, warned) -> None:
    """A sample of types: str, bool, float, Path, a Literal, an optional."""
    monkeypatch.setenv("ATHENA_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("ATHENA_KEEP_AUDIO", "true")
    monkeypatch.setenv("ATHENA_SILENCE_STOP_SECONDS", "12.5")
    monkeypatch.setenv("ATHENA_DATA_DIR", "/srv/athena")
    monkeypatch.setenv("ATHENA_DIARIZER", "pyannote")
    monkeypatch.setenv("ATHENA_MCP_TOKEN", "m" * 20)
    s = settings()
    assert (s.ingest_token, s.keep_audio, s.silence_stop_seconds, s.data_dir, s.diarizer,
            s.mcp_token) == (TOKEN, True, 12.5, Path("/srv/athena"), "pyannote", "m" * 20)
    assert s.vad_threshold == 0.35  # the diarizer's defaults follow the new name too


def test_new_name_typo_still_fails(monkeypatch, warned) -> None:
    monkeypatch.setenv("ATHENA_DIARIZER", "wisper")
    with pytest.raises(ValidationError) as e:
        settings(ingest_token=TOKEN)
    assert "diarizer" in str(e.value)


def test_ingest_token_new_name_in_env_or_dotenv(monkeypatch, tmp_path, warned) -> None:
    dotenv = tmp_path / "test.env"
    dotenv.write_text(f"ATHENA_INGEST_TOKEN={TOKEN}\n")
    assert settings(dotenv).ingest_token == TOKEN
    monkeypatch.setenv("ATHENA_INGEST_TOKEN", "e" * 20)
    assert settings(dotenv).ingest_token == "e" * 20


def test_placeholder_token_refused_under_either_name(monkeypatch, warned) -> None:
    monkeypatch.setenv("ATHENA_INGEST_TOKEN", "change-me-0123456789")
    with pytest.raises(ValidationError) as e:
        settings()
    assert "CONVO_INGEST_TOKEN (or ATHENA_INGEST_TOKEN) still has the placeholder" in str(e.value)


def test_both_names_different_values_warn_once_without_the_value(
        monkeypatch, caplog, warned) -> None:
    secret = "s" * 24
    monkeypatch.setenv("ATHENA_INGEST_TOKEN", secret)
    monkeypatch.setenv("CONVO_INGEST_TOKEN", TOKEN)
    with caplog.at_level(logging.WARNING, logger="athena.config"):
        assert settings().ingest_token == secret
        settings()
    msgs = [r.getMessage() for r in caplog.records if r.name == "athena.config"]
    assert msgs == ["ATHENA_INGEST_TOKEN and CONVO_INGEST_TOKEN are both set, to different "
                    "values; using ATHENA_INGEST_TOKEN from the environment"]
    assert secret not in msgs[0] and TOKEN not in msgs[0]


def test_both_names_same_value_do_not_warn(monkeypatch, caplog, warned) -> None:
    monkeypatch.setenv("ATHENA_PORT", "8100")
    monkeypatch.setenv("CONVO_PORT", "8100")
    with caplog.at_level(logging.WARNING, logger="athena.config"):
        assert settings(ingest_token=TOKEN).port == 8100
    assert not [r for r in caplog.records if r.name == "athena.config"]


def test_env_setting(monkeypatch) -> None:
    assert config.env_setting("DATA_DIR") is None
    assert config.env_setting("DATA_DIR", "data") == "data"
    monkeypatch.setenv("CONVO_DATA_DIR", "/old")
    assert config.env_setting("DATA_DIR") == "/old"
    monkeypatch.setenv("ATHENA_DATA_DIR", "/new")
    assert config.env_setting("DATA_DIR") == "/new"


# ------------------------------------------------------------------ database file
def test_fresh_data_dir_gets_athena_db(tmp_path, warned) -> None:
    s = settings(ingest_token=TOKEN, data_dir=tmp_path)
    assert s.db_path == tmp_path / "athena.db"
    Store(s.db_path).close()
    assert (tmp_path / "athena.db").is_file() and not (tmp_path / "convo.db").exists()
    assert settings(ingest_token=TOKEN, data_dir=tmp_path).db_path == tmp_path / "athena.db"


def test_only_convo_db_is_opened_as_is(tmp_path, warned) -> None:
    Store(tmp_path / "convo.db").close()
    before = sorted(p.name for p in tmp_path.iterdir())
    s = settings(ingest_token=TOKEN, data_dir=tmp_path)
    assert s.db_path == tmp_path / "convo.db"
    store = Store(s.db_path)
    store.close()
    assert not (tmp_path / "athena.db").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing moved or added
    # sidecar files (-wal, -shm, the init lock) follow the chosen file
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("athena")]


def test_both_files_athena_db_wins_and_warns_once(tmp_path, caplog, warned) -> None:
    (tmp_path / "convo.db").write_bytes(b"")
    (tmp_path / "athena.db").write_bytes(b"")
    with caplog.at_level(logging.WARNING, logger="athena.config"):
        s = settings(ingest_token=TOKEN, data_dir=tmp_path)
        assert s.db_path == tmp_path / "athena.db"
        assert s.db_path == tmp_path / "athena.db"
    msgs = [r.getMessage() for r in caplog.records if r.name == "athena.config"]
    assert len(msgs) == 1 and "convo.db is ignored" in msgs[0], msgs
    assert (tmp_path / "convo.db").exists()  # left alone


@pytest.mark.parametrize("name", ["DB_FILE", "db_file"])
def test_explicit_db_file_wins(name, monkeypatch, tmp_path, warned) -> None:
    (tmp_path / "convo.db").write_bytes(b"")
    (tmp_path / "athena.db").write_bytes(b"")
    monkeypatch.setenv(f"ATHENA_{name}", "mine.db")
    assert settings(ingest_token=TOKEN, data_dir=tmp_path).db_path == tmp_path / "mine.db"
    monkeypatch.setenv(f"ATHENA_{name}", "/elsewhere/convo.db")
    assert settings(ingest_token=TOKEN, data_dir=tmp_path).db_path == Path(
        "/elsewhere/convo.db")
    monkeypatch.delenv(f"ATHENA_{name}")
    monkeypatch.setenv("CONVO_DB_FILE", "convo.db")  # the old name too
    assert settings(ingest_token=TOKEN, data_dir=tmp_path).db_path == tmp_path / "convo.db"


def test_resolve_db_path_never_creates_a_file(tmp_path) -> None:
    assert config.resolve_db_path(tmp_path) == tmp_path / "athena.db"
    assert list(tmp_path.iterdir()) == []
