"""Server configuration, loaded from environment variables / server/.env.

Every variable is prefixed with ``CONVO_``. See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONVO_", env_file=".env", extra="ignore")

    # --- ingestion / auth ---
    # Shared token every device sends as `Authorization: Bearer <token>`.
    # No default on purpose: the server refuses to start without one.
    ingest_token: str = Field(min_length=16)
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000

    # --- session control ---
    # Server tells the device to stop after this much trailing silence.
    silence_stop_seconds: float = 300.0
    # Hard cap on a single recording session (device should also enforce it).
    max_session_seconds: float = 4 * 3600.0
    # Sessions with no new chunk for this long are finalized and processed.
    session_idle_seconds: float = 600.0

    # --- pipeline ---
    vad_backend: str = "silero"  # "silero" (production) or "energy" (tests only)
    conversation_gap_seconds: float = 180.0
    keep_audio: bool = False
    language: str = "en"
    timezone: str = "UTC"  # IANA name used for daily digests, e.g. Europe/Berlin
    run_worker: bool = True  # background thread: finalize idle sessions, process, digest

    # whisper.cpp: either a running `whisper-server` URL or a `whisper-cli` binary + model.
    whisper_server_url: str | None = None  # e.g. http://127.0.0.1:8178
    whisper_cli: str | None = None  # e.g. /opt/whisper.cpp/build/bin/whisper-cli
    whisper_model: str | None = None  # e.g. /opt/whisper.cpp/models/ggml-large-v3-turbo.bin
    # Words whisper should spell right (names, places, jargon), as a short sentence or a
    # comma list. Names of known speakers are added automatically.
    whisper_prompt: str = ""
    # Temperature step for whisper's fallback when a decode looks wrong (0 = no fallback).
    whisper_temperature_inc: float = 0.2
    # Decoding, sent with every request (whisper.cpp names; README "Recognition eval").
    whisper_beam_size: int = -1  # > 1 = beam search with this many beams, else greedy
    whisper_best_of: int = 2  # candidates at temperature > 0 (the fallback)
    whisper_no_speech_thold: float = 0.6
    whisper_logprob_thold: float = -1.0
    whisper_entropy_thold: float = 2.4
    # Suppress non-speech tokens ([LAUGHTER], music notes, "(muffled speaking)"); fewer
    # dropped words on far-field audio in the recognition eval.
    whisper_suppress_non_speech: bool = True
    # Keep the prompt for every 30 s window of a long clip, not just the first one.
    whisper_carry_initial_prompt: bool = False

    # llama-server (OpenAI-compatible)
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "default"
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 600.0
    # The worker retries a failed summary (e.g. llama-server was down) once its last
    # attempt is this old. `convo-server summarize-pending` retries them right away.
    summary_retry_seconds: float = 300.0

    # Optional embeddings endpoint (llama-server started with --embeddings).
    # When unset, search falls back to SQLite FTS5 keyword search only.
    embed_base_url: str | None = None
    embed_model: str = "default"

    # Diarization
    diarization: bool = True
    # "ecapa": cluster one voiceprint per transcript piece (no extra install).
    # "pyannote": pyannote.audio community-1 pipeline, local (extra `pyannote`).
    diarizer: str = "ecapa"
    # Hugging Face id or a local directory. Cloud (pyannoteAI) pipelines are refused.
    pyannote_model: str = "pyannote-community/speaker-diarization-community-1"
    pyannote_device: str = "auto"  # auto (cuda if available), cuda or cpu
    speaker_match_threshold: float = 0.50  # cosine similarity to enrolled voice
    speaker_cluster_threshold: float = 0.45  # cosine similarity to merge clusters
    # After a speaker is named, unnamed speakers in other conversations whose voice is at
    # least this close are renamed too (stricter than the match threshold).
    speaker_relabel_threshold: float = 0.60
    # Owner hint: the wearer is usually the loudest voice. A cluster that is at least
    # owner_hint_db louder than all others and within owner_hint_margin below the match
    # threshold of the owner's voice is labelled as the owner. 0 margin = off.
    owner_hint_margin: float = 0.10
    owner_hint_db: float = 3.0
    owner_name: str = "Me"
    # Where downloaded model weights are cached (speaker models). Default: <data_dir>/models.
    models_dir: Path | None = None

    # --- knowledge layer ---
    # Base URL used in transcript links handed to the frontend, e.g. the server's
    # Tailscale address: http://100.64.0.10:8000. Defaults to http://<host>:<port>.
    public_url: str | None = None
    # MCP over Streamable HTTP (for frontends that can't spawn a stdio process, e.g.
    # Odysseus in Docker). stdio needs no token; HTTP refuses to start without one.
    mcp_token: str | None = None
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001

    @property
    def base_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/")

    @field_validator("ingest_token", "mcp_token")
    @classmethod
    def _not_placeholder(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is not None and v.lower().startswith("change-me"):
            raise ValueError(f"CONVO_{info.field_name.upper()} still has the placeholder value")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "convo.db"

    @property
    def model_cache(self) -> Path:
        return self.models_dir or self.data_dir / "models"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
