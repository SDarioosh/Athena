"""Recognition: whisper prompt/fallback, voice samples, relabelling, speaker fixes."""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
from conftest import tone
from test_pipeline import ingest

from convo_server.transcribe import (
    TranscriptPiece,
    WhisperCliTranscriber,
    WhisperServerTranscriber,
    _clean,
)


def test_whisper_server_gets_prompt_and_temperature_fallback():
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "text": "hi"}]})

    t = WhisperServerTranscriber("http://whisper.test", transport=httpx.MockTransport(handler),
                                 temperature_inc=0.3)
    assert [p.text for p in t.transcribe(tone(300, 1), "Jenn, Shahyah.")] == ["hi"]
    body = seen[0]
    assert b'name="prompt"\r\n\r\nJenn, Shahyah.' in body
    assert b'name="temperature_inc"\r\n\r\n0.3' in body
    t.transcribe(tone(300, 1))
    assert b'name="prompt"' not in seen[1]


def test_whisper_cli_command_has_prompt_and_fallback():
    t = WhisperCliTranscriber("whisper-cli", "model.bin", temperature_inc=0.2)
    cmd = t.command(Path("a.wav"), Path("out"), "Barvis, Jenn.")
    assert cmd[cmd.index("-tpi") + 1] == "0.2"
    assert cmd[-2:] == ["--prompt", "Barvis, Jenn."]
    assert "--prompt" not in t.command(Path("a.wav"), Path("out"))


def test_non_speech_tags_are_dropped():
    pieces = [TranscriptPiece(0, 1, t) for t in
              ["[BLANK_AUDIO]", " (upbeat music) ", "*laughs*", "[MUSIC] [MUSIC]",
               "So [inaudible] then", "Real words."]]
    assert [p.text for p in _clean(pieces)] == ["So [inaudible] then", "Real words."]


def test_prompt_has_vocabulary_and_known_names(pipeline):
    pipeline.settings.whisper_prompt = "Barvis, Tailscale."
    pipeline.store.put_speaker("Jenn", np.ones(16, np.float32), is_owner=False)
    assert pipeline.whisper_prompt() == "Barvis, Tailscale. Jenn."
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.transcriber.prompts == ["Barvis, Tailscale. Jenn."]
