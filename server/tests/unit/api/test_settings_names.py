"""Where settings and the database file come from: the old CONVO_* names and an existing
convo.db keep working."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.config import Settings

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
