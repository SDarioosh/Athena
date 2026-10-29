"""Settings: backend choices are checked at startup; .env.example lists every setting."""

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena import meetings
from athena.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
TOKEN = "t" * 32


def test_every_setting_is_in_env_example() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*CONVO_([A-Z0-9_]+)=", text, re.MULTILINE))
    missing = sorted(f"CONVO_{name.upper()}" for name in Settings.model_fields
                     if name.upper() not in listed)
    assert not missing, f"add to server/.env.example: {', '.join(missing)}"


CHOICES = {
    "vad_backend": ("silero", ["silero", "energy"]),
    "transcriber": ("whisper", ["whisper", "parakeet"]),
    "diarizer": ("ecapa", ["ecapa", "pyannote"]),
    "speaker_embedder": ("ecapa", ["ecapa", "pyannote"]),
}
# checked when a meeting is requested; a wrong value only warns at startup
MEETING = {
    "meeting_bot_runner": ("auto", ["auto", "systemd", "plain"]),
    "meeting_track_codec": ("opus", ["opus", "wav"]),
    "zoom_join_token": ("auto", ["auto", "zak", "obf", "none"]),
}


@pytest.mark.parametrize("field", sorted(CHOICES | MEETING))
def test_backend_choice_defaults_and_values(field: str, monkeypatch) -> None:
    monkeypatch.delenv(f"CONVO_{field.upper()}", raising=False)
    default, allowed = (CHOICES | MEETING)[field]
    assert getattr(Settings(ingest_token=TOKEN, _env_file=None), field) == default
    for value in allowed:
        monkeypatch.setenv(f"CONVO_{field.upper()}", value)
        assert getattr(Settings(ingest_token=TOKEN, _env_file=None), field) == value


@pytest.mark.parametrize("field", sorted(CHOICES))
def test_backend_choice_typo_fails_at_startup(field: str, monkeypatch) -> None:
    monkeypatch.setenv(f"CONVO_{field.upper()}", "wisper")
    with pytest.raises(ValidationError) as e:
        Settings(ingest_token=TOKEN, _env_file=None)
    assert field in str(e.value)


@pytest.mark.parametrize("field", sorted(MEETING))
def test_meeting_choice_typo_only_warns_at_startup(field: str, monkeypatch, caplog) -> None:
    monkeypatch.setenv(f"CONVO_{field.upper()}", "wisper")
    with caplog.at_level(logging.WARNING, logger="athena.config"):
        s = Settings(ingest_token=TOKEN, _env_file=None)
    assert getattr(s, field) == "wisper"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"CONVO_{field.upper()}='wisper'" in w for w in warnings), warnings


def test_valid_meeting_choices_do_not_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="athena.config"):
        Settings(ingest_token=TOKEN, _env_file=None)
    assert not [r for r in caplog.records if r.name == "athena.config"]


@pytest.mark.parametrize("field", sorted(MEETING))
def test_meeting_request_still_refuses_a_typo(field: str, tmp_path, monkeypatch) -> None:
    bot = tmp_path / "bot"
    bot.write_text("#!/bin/sh\n")
    bot.chmod(0o755)
    monkeypatch.setenv(f"CONVO_{field.upper()}", "wisper")
    s = Settings(ingest_token=TOKEN, _env_file=None, meeting_bot_path=bot,
                 zoom_sdk_key="key", zoom_sdk_secret="secret")
    with pytest.raises(meetings.MeetingError) as e:
        meetings.check_settings(s)
    assert e.value.status == 501
    assert f"CONVO_{field.upper()} must be" in e.value.detail
