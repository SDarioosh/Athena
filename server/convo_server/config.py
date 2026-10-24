"""Server configuration, loaded from environment variables / server/.env.

Every variable is prefixed with ``CONVO_``. See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator, model_validator
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
    # Silero speech threshold, the silence that ends a speech span, and padding around
    # each span (Silero's default is 30 ms; Omi uses 300 ms pre-roll). Ignored by the
    # energy backend.
    # Unset: 0.35 with CONVO_DIARIZER=pyannote (measured on the server GPU 2026-10-24:
    # lets more of the others' far speech through, WER about -0.9), else Silero's 0.5.
    vad_threshold: float | None = None
    vad_min_silence_ms: int = 500
    vad_speech_pad_ms: int = 30
    # Bounded peak gain on a COPY of each chunk before VAD scores it, so quiet far-field
    # speakers aren't dropped as silence (1 = off). Stored audio is unchanged.
    vad_agc_max_gain: float = 1.0
    vad_agc_target: float = 0.8
    # Same gain on each clip sent to the transcriber, only when its peak is <= 0.4 of
    # full scale (1 = off). The diarizer always gets the ungained audio.
    asr_agc_max_gain: float = 1.0
    conversation_gap_seconds: float = 180.0
    keep_audio: bool = False
    # Retention: delete transcripts (and any kept audio) of conversations that ended more
    # than this many days ago; summaries, facts and action items stay. 0 = keep forever
    # (the default, the owner's decision). Pending and failed conversations are kept
    # until they are summarized.
    transcript_retention_days: int = 0
    language: str = "en"
    timezone: str = "UTC"  # IANA name used for daily digests, e.g. Europe/Berlin
    run_worker: bool = True  # background thread: finalize idle sessions, process, digest
    # To-do lists left out of the daily digest (comma list, any case, with or without
    # "list"): a shopping list is not news for the day.
    digest_skip_lists: str = "Shopping,Groceries,Grocery"

    # Speech-to-text model: "whisper" (whisper.cpp, below) or "parakeet" (Parakeet TDT
    # 0.6B v3 in-process via sherpa-onnx, extra `parakeet`; being measured against whisper).
    transcriber: str = "whisper"
    # Unpacked sherpa-onnx export, e.g. .../sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
    parakeet_model_dir: Path | None = None
    parakeet_threads: int = 4
    # Re-decode stretches of speech ≥ 1 s inside a chunk where Parakeet emitted no
    # tokens, and merge the words in. Off until measured on eval/recognition.py.
    parakeet_redecode_holes: bool = False

    # whisper.cpp: either a running `whisper-server` URL or a `whisper-cli` binary + model.
    whisper_server_url: str | None = None  # e.g. http://127.0.0.1:8178
    whisper_cli: str | None = None  # e.g. /opt/whisper.cpp/build/bin/whisper-cli
    whisper_model: str | None = None  # e.g. /opt/whisper.cpp/models/ggml-large-v3-turbo.bin
    # Words whisper should spell right (names, places, jargon), as a short sentence or a
    # comma list. Names of known speakers are added automatically.
    whisper_prompt: str = ""
    # Add the names of known speakers to that prompt.
    whisper_prompt_names: bool = True
    # Temperature step for whisper's fallback when a decode looks wrong (0 = no fallback).
    whisper_temperature_inc: float = 0.2
    # Drop text whisper invents on noise ("Thank you." on a cough) and cut repetition
    # loops (transcribe.HallucinationFilter; README "Recognition eval").
    whisper_drop_hallucinations: bool = True
    # A segment that is only a stock phrase is dropped below this mean token log-prob.
    whisper_drop_phrase_logprob: float = -0.7
    # Words, phrases or segments repeated more than this many times in a row are cut.
    whisper_max_repeats: int = 4
    # How speech is cut into clips for whisper (convo_server/clips.py). VAD regions are
    # padded by whisper_pad_seconds and packed into clips of up to whisper_window_seconds;
    # silence between regions inside a clip is shortened to whisper_window_gap_seconds.
    # whisper_window_seconds=0 sends every region alone.
    whisper_window_seconds: float = 28.0
    whisper_pad_seconds: float = 0.25
    whisper_window_gap_seconds: float = 1.0
    # Last N words of the previous clip's text, added to the prompt after the vocabulary
    # (0 = off).
    whisper_context_words: int = 0
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
    # pyannote's clustering threshold (unset = the pipeline's own, 0.6 for community-1):
    # lower splits voices into more speakers, higher joins them. Speaker count bounds per
    # recording (0 = none). Not measured on the server yet: leave unset by default.
    # Voiceprints for naming speakers: "ecapa" (SpeechBrain, default) or "pyannote" (the
    # pyannote pipeline's own WeSpeaker model; CONVO_DIARIZER=pyannote only). Switching
    # needs a new enrollment: voices of the other model never match.
    speaker_embedder: str = "ecapa"
    pyannote_cluster_threshold: float | None = None
    # VBx clustering (community-1): how many speakers come out. Unset = the pipeline's own
    # (Fa 0.07, Fb 0.8); a lower Fb or a higher Fa keeps more speakers apart.
    pyannote_vbx_fa: float | None = None
    pyannote_vbx_fb: float | None = None
    pyannote_min_speakers: int = 0
    pyannote_max_speakers: int = 0
    speaker_match_threshold: float = 0.50  # cosine similarity to enrolled voice
    speaker_cluster_threshold: float = 0.45  # cosine similarity to merge clusters
    # A speaker with a single line joins the closest speaker with several lines when its
    # voice is at least this similar (0 = off).
    speaker_absorb_threshold: float = 0.30
    # pyannote only: split a transcript line where pyannote's turns change speaker
    # (needs word times from whisper-server's verbose_json). Unset: on with
    # CONVO_DIARIZER=pyannote (server GPU eval 2026-10-24), off otherwise.
    speaker_split_lines: bool | None = None
    # Check each named line's own voice against the enrolled voices; a line that fits
    # another enrolled person at least as well is labelled "Unknown" instead of keeping a
    # probably wrong name. Unset: on with CONVO_DIARIZER=pyannote (with the line split it
    # cut wrong names 30-70 % in the server GPU eval), off otherwise.
    speaker_line_check: bool | None = None
    # With the line check: a named line whose voice scores below this against its own
    # name (cosine, like speaker_match_threshold) is labelled "Unknown" too: someone new.
    # 0 = off.
    speaker_line_floor: float = 0.0
    # Line splitting: the shortest part a split leaves (shorter ones join a neighbour).
    # Unset: 0.3 with CONVO_DIARIZER=pyannote, else 1.5.
    speaker_split_min_seconds: float | None = None
    # Name a speaker only if its best voice beats the runner-up voice by this much
    # (0 = off), and (pyannote) only with this much speech (0 = off).
    speaker_match_margin: float = 0.0
    speaker_name_min_seconds: float = 0.0
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
    # A fact is "confirmed" (list_facts confirmed=true; the hub copies these into its
    # memory) once it was said in at least this many distinct conversations, on at least
    # this many distinct dates in CONVO_TIMEZONE, and no newer fact has replaced it.
    fact_confirm_sources: int = Field(default=3, ge=1)
    fact_confirm_days: int = Field(default=2, ge=1)

    @property
    def base_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/")

    @model_validator(mode="after")
    def _diarizer_defaults(self) -> Settings:
        """Settings left unset get the defaults measured for the chosen diarizer (pyannote:
        the 4.3 capture-quality eval, docs/omi-audio-quality.md §8.3). A value set in the
        environment always wins."""
        py = self.diarizer == "pyannote"
        if self.vad_threshold is None:
            self.vad_threshold = 0.35 if py else 0.5
        if self.speaker_split_lines is None:
            self.speaker_split_lines = py
        if self.speaker_split_min_seconds is None:
            self.speaker_split_min_seconds = 0.3 if py else 1.5
        if self.speaker_line_check is None:
            self.speaker_line_check = py
        return self

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
