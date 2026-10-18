"""Command line: ``convo-server serve | process | digest | summarize-pending``."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings, get_settings
from .diarize import Diarizer, EcapaEmbedder, load_voices
from .llm import LLMClient, Summarizer
from .pipeline import Pipeline
from .search import OpenAIEmbedder
from .store import Store
from .transcribe import Transcriber, WhisperCliTranscriber, WhisperServerTranscriber
from .vad import make_vad


def build_pipeline(settings: Settings) -> Pipeline:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    transcriber: Transcriber | None = None
    if settings.whisper_server_url:
        transcriber = WhisperServerTranscriber(settings.whisper_server_url, settings.language)
    elif settings.whisper_cli and settings.whisper_model:
        transcriber = WhisperCliTranscriber(settings.whisper_cli, settings.whisper_model,
                                            settings.language)
    diarizer = None
    if settings.diarization:
        diarizer = Diarizer(EcapaEmbedder(settings.data_dir / "models"), load_voices(store),
                            settings.speaker_match_threshold, settings.speaker_cluster_threshold)
    llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                    settings.llm_timeout_seconds)
    embedder = (OpenAIEmbedder(settings.embed_base_url, settings.embed_model)
                if settings.embed_base_url else None)
    return Pipeline(settings, store, make_vad(settings.vad_backend), transcriber, diarizer,
                    Summarizer(llm, settings.owner_name), embedder)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="convo-server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the HTTP API (+ background worker)")
    sub.add_parser("process", help="finalize idle sessions and process everything pending")
    d = sub.add_parser("digest", help="(re)build the daily digest")
    d.add_argument("--date", help="YYYY-MM-DD, default yesterday")
    sub.add_parser("summarize-pending", help="retry conversations without a summary")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
