"""Settings: backend choices are checked at startup; .env.example lists every setting."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from convo_server.config import Settings

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
    "meeting_bot_runner": ("auto", ["auto", "systemd", "plain"]),
    "meeting_track_codec": ("opus", ["opus", "wav"]),
    "zoom_join_token": ("auto", ["auto", "zak", "obf", "none"]),
}


@pytest.mark.parametrize("field", sorted(CHOICES))
def test_backend_choice_defaults_and_values(field: str, monkeypatch) -> None:
    monkeypatch.delenv(f"CONVO_{field.upper()}", raising=False)
    default, allowed = CHOICES[field]
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
