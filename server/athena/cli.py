"""Command line: ``convo-server serve | process | digest | summarize-pending``."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import llm_settings
from .config import Settings, get_settings
from .db.store import Store
from .diarize import Diarizer, EcapaEmbedder, load_voices
from .pipeline import Pipeline
from .search import OpenAIEmbedder
from .summarize.client import LLMClient
from .summarize.summarizer import Summarizer
from .transcribe import (
    DecodeOptions,
    HallucinationFilter,
    ParakeetTranscriber,
    Transcriber,
    WhisperCliTranscriber,
    WhisperServerTranscriber,
)
from .vad import make_vad


def decode_options(settings: Settings) -> DecodeOptions:
    return DecodeOptions(temperature_inc=settings.whisper_temperature_inc,
                         beam_size=settings.whisper_beam_size,
                         best_of=settings.whisper_best_of,
                         no_speech_thold=settings.whisper_no_speech_thold,
                         logprob_thold=settings.whisper_logprob_thold,
                         entropy_thold=settings.whisper_entropy_thold,
                         suppress_non_speech=settings.whisper_suppress_non_speech,
                         carry_initial_prompt=settings.whisper_carry_initial_prompt)


def build_transcriber(settings: Settings) -> Transcriber | None:
    if settings.transcriber == "parakeet":
        if not settings.parakeet_model_dir:
            raise RuntimeError("CONVO_TRANSCRIBER=parakeet needs CONVO_PARAKEET_MODEL_DIR")
        return ParakeetTranscriber(settings.parakeet_model_dir, settings.parakeet_threads,
                                   redecode_holes=settings.parakeet_redecode_holes)
    if settings.transcriber != "whisper":
        raise RuntimeError(f"unknown CONVO_TRANSCRIBER {settings.transcriber!r} "
                           "(whisper or parakeet)")
    t: WhisperServerTranscriber | WhisperCliTranscriber
    if settings.whisper_server_url:
        t = WhisperServerTranscriber(settings.whisper_server_url, settings.language,
                                     decode=decode_options(settings))
    elif settings.whisper_cli and settings.whisper_model:
        t = WhisperCliTranscriber(settings.whisper_cli, settings.whisper_model,
                                  settings.language, decode=decode_options(settings))
    else:
        return None
    t.hallucinations = HallucinationFilter(
        phrase_logprob=settings.whisper_drop_phrase_logprob,
        max_repeats=settings.whisper_max_repeats,
    ) if settings.whisper_drop_hallucinations else None
    return t


def build_diarizer(settings: Settings, store: Store) -> Diarizer | None:
    if not settings.diarization:
        return None
    args = dict(match_threshold=settings.speaker_match_threshold,
                cluster_threshold=settings.speaker_cluster_threshold,
                owner_hint_margin=settings.owner_hint_margin,
                owner_hint_db=settings.owner_hint_db,
                absorb_threshold=settings.speaker_absorb_threshold)
    if settings.speaker_embedder not in ("ecapa", "pyannote"):
        raise SystemExit("CONVO_SPEAKER_EMBEDDER must be ecapa or pyannote")
    if settings.speaker_embedder == "pyannote" and settings.diarizer != "pyannote":
        raise SystemExit("CONVO_SPEAKER_EMBEDDER=pyannote needs CONVO_DIARIZER=pyannote")
    embedder = (EcapaEmbedder(settings.model_cache) if settings.speaker_embedder == "ecapa"
                else None)  # pyannote: built from the loaded pipeline below
    diarizer = _build_diarizer(settings, store, embedder, args)
    diarizer.match_margin = settings.speaker_match_margin
    diarizer.name_min_seconds = settings.speaker_name_min_seconds
    diarizer.split_min_seconds = settings.speaker_split_min_seconds
    return diarizer


def _build_diarizer(settings: Settings, store: Store, embedder: EcapaEmbedder | None,
                    args: dict) -> Diarizer:
    if settings.diarizer == "pyannote":
        from .diarize_pyannote import PyannoteDiarizer, PyannoteSegmenter

        segmenter = PyannoteSegmenter(settings.pyannote_model, settings.model_cache / "pyannote",
                                      settings.pyannote_device,
                                      cluster_threshold=settings.pyannote_cluster_threshold,
                                      min_speakers=settings.pyannote_min_speakers,
                                      max_speakers=settings.pyannote_max_speakers,
                                      vbx_fa=settings.pyannote_vbx_fa,
                                      vbx_fb=settings.pyannote_vbx_fb)
        if embedder is None:
            from .diarize_pyannote import PyannoteEmbedder

            embedder = PyannoteEmbedder(segmenter)
        return PyannoteDiarizer(embedder, load_voices(store), segmenter, **args)
    if settings.diarizer != "ecapa":
        raise SystemExit("CONVO_DIARIZER must be ecapa or pyannote")
    return Diarizer(embedder, load_voices(store), **args)


def build_pipeline(settings: Settings) -> Pipeline:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                    settings.llm_timeout_seconds)
    llm_settings.apply_stored(llm, store)  # a model chosen in the hub's settings
    embedder = (OpenAIEmbedder(settings.embed_base_url, settings.embed_model)
                if settings.embed_base_url else None)
    return Pipeline(settings, store, make_vad(settings.vad_backend, settings),
                    build_transcriber(settings), build_diarizer(settings, store),
                    Summarizer(llm, settings.owner_name, timezone=settings.timezone,
                               digest_skip_lists=settings.digest_skip_lists,
                               fact_min_score=settings.fact_min_score,
                               max_facts=settings.max_facts_per_conversation),
                    embedder)


def zoom_login(settings: Settings, *, status_only: bool = False,
               ask: Callable[[str], str] = input, transport: object = None) -> int:
    """``convo-server zoom-login``: print the Zoom authorize URL, take the redirect URL (or
    its code) pasted back, save the refresh token. Nothing listens for the redirect: the
    server may be headless. Tokens are never printed."""
    import asyncio

    from .meetings.bot import zoom_signin

    path = zoom_signin.token_file(settings)
    if status_only:
        print(f"signed in ({path})" if zoom_signin.signed_in(settings) else "not signed in")
        print(f"join token for the next meeting: {zoom_signin.join_mode(settings)}")
        return 0
    try:
        login = zoom_signin.start_login(settings)
    except zoom_signin.SigninNeeded as e:
        print(f"error: {e}")
        return 2
    print("1. Open this URL in a browser and sign in with your Zoom account:\n")
    print(f"   {login.url}\n")
    print("2. Allow the app. The browser then goes to the redirect URL (the page itself may")
    print("   fail to load; that's fine). Copy the whole address from the address bar.\n")
    try:
        code = zoom_signin.code_from_paste(ask("3. Paste it here: "), login.state)
        scope = asyncio.run(zoom_signin.finish_login(settings, login, code,
                                                     transport=transport))  # type: ignore[arg-type]
    except (ValueError, zoom_signin.SigninNeeded, zoom_signin.ZoomUnavailable) as e:
        print(f"error: {e}")
        return 1
    print(f"\nSigned in. Refresh token saved to {path} (mode 0600).")
    if scope:
        print(f"Scopes: {scope}")
    if "user:read:token" not in scope.split():
        print("warning: the app lacks the user:read:token scope; Zoom will refuse join tokens")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="convo-server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the HTTP API (+ background worker)")
    sub.add_parser("process", help="finalize idle sessions and process everything pending")
    d = sub.add_parser("digest", help="(re)build the daily digest")
    d.add_argument("--date", help="YYYY-MM-DD, default yesterday")
    sub.add_parser("summarize-pending", help="summarize pending, retry failed now")
    m = sub.add_parser("mcp", help="MCP server for knowledge frontends (stdio by default)")
    m.add_argument("--http", action="store_true",
                   help="serve Streamable HTTP on CONVO_MCP_HOST:CONVO_MCP_PORT/mcp")
    z = sub.add_parser("zoom-login", help="sign Athena in to its Zoom account once "
                       "(for meetings in other Zoom accounts)")
    z.add_argument("--status", action="store_true", help="only say whether it is signed in")
    args = parser.parse_args(argv)

    settings = get_settings()
    # logs go to stderr; stdout is the MCP stdio channel
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "mcp":
        from .mcp_server import run

        store = Store(settings.db_path)
        embedder = (OpenAIEmbedder(settings.embed_base_url, settings.embed_model)
                    if settings.embed_base_url else None)
        run(settings, store, embedder, http=args.http)
        return

    if args.cmd == "zoom-login":
        raise SystemExit(zoom_login(settings, status_only=args.status))

    pipeline = build_pipeline(settings)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(pipeline), host=settings.host, port=settings.port)
    elif args.cmd == "process":
        pipeline.finalize_idle()
        print(f"processed {pipeline.process_pending()} session(s)")
    elif args.cmd == "digest":
        tz = ZoneInfo(settings.timezone)
        date = args.date or (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
        digest = pipeline.make_digest(date)
        print(digest if digest else f"no summarized conversations on {date}")
    elif args.cmd == "summarize-pending":
        print(f"summarized {pipeline.summarize_pending()} conversation(s)")


if __name__ == "__main__":
    main()
