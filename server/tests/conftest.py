from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from athena.audio import SAMPLE_RATE, to_wav_bytes
from athena.config import Settings
from athena.db.store import Store
from athena.diarize import Diarizer, load_voices
from athena.pipeline import Pipeline
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from athena.transcribe import TranscriptPiece
from athena.vad import EnergyVAD

TOKEN = "test-token-0123456789abcdef"


def tone(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def wav(*parts: np.ndarray) -> bytes:
    return to_wav_bytes(np.concatenate(parts))


class FakeTranscriber:
    """Returns one piece per call, naming the dominant tone so tests can assert on text."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.prompts: list[str] = []

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        if self.fail:
            raise RuntimeError("whisper down")
        self.calls += 1
        self.prompts.append(prompt)
        freq = dominant_freq(audio)
        return [TranscriptPiece(0.0, audio.size / SAMPLE_RATE, f"words at {freq} hz")]


def dominant_freq(audio: np.ndarray) -> int:
    spec = np.abs(np.fft.rfft(audio))
    return int(round(np.fft.rfftfreq(audio.size, 1 / SAMPLE_RATE)[int(np.argmax(spec))], -2))


class ToneEmbedder:
    """Speaker 'voice' = dominant frequency bucket. Deterministic stand-in for ECAPA."""

    def embed(self, audio: np.ndarray) -> np.ndarray:
        v = np.zeros(16, dtype=np.float32)
        v[min(15, dominant_freq(audio) // 100)] = 1.0
        v += 0.01
        return v / np.linalg.norm(v)


def fake_llm_transport(summary: dict | None = None, digest: dict | None = None,
                       log: list | None = None, triage: dict | None = None,
                       links: list | None = None,
                       scores: dict[str, int] | None = None) -> httpx.MockTransport:
    """Fake llama-server: answers each structured call by its schema name. The fact
    review scores every fact 5 unless ``scores`` (fact text -> score) says otherwise."""
    summary = summary or {
        "title": "Planning the trip",
        "summary": "Alice and the user planned a trip to Lisbon.",
        "key_points": ["Trip to Lisbon in May"],
        "action_items": [{"text": "Book flights", "owner": "Me", "due": "Friday",
                          "due_date": "2025-10-10", "at_seconds": 1}],
        "people": ["Alice", "Me"],
        "topics": ["travel"],
        "facts": [{"subject": "Alice", "fact": "Alice is vegetarian", "at_seconds": 2}],
    }
    digest = digest or {"headline": "A day of plans", "summary": "Trip planning.",
                        "highlights": ["Lisbon"], "open_action_items": ["Book flights"],
                        "people": ["Alice"]}
    triage = triage or {"discard": False, "reason": "none", "kind": "casual"}
    notes = {k: summary[k] for k in ("title", "summary", "key_points", "people", "topics")}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if log is not None:
            log.append(body)
        name = body["response_format"]["json_schema"]["name"]
        if name == "FactReview":
            asked = json.loads(body["messages"][-1]["content"])["facts"]
            content = {"scores": [{"number": f["number"],
                                   "score": (scores or {}).get(f["fact"], 5)}
                                  for f in asked]}
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(content)}}]})
        content = {
            "DailyDigest": digest, "Triage": triage, "SummaryNotes": notes,
            "ActionItemList": {"action_items": summary["action_items"]},
            "FactList": {"facts": summary["facts"]},
            "FactLinks": {"links": links or []},
        }.get(name, summary)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(content)}}]})

    return httpx.MockTransport(handler)


def make_summarizer(summary: dict | None = None, *, scores: dict[str, int] | None = None,
                    log: list | None = None, **opts) -> Summarizer:
    """The owner "Me"'s summarizer in Europe/Berlin, answered by the fake llama-server
    (``fake_llm_transport`` with ``summary``, ``scores`` and ``log``); ``opts`` go to
    ``Summarizer`` (e.g. ``min_words=1``)."""
    llm = LLMClient("http://llm.test/v1", "m",
                    transport=fake_llm_transport(summary=summary, log=log, scores=scores))
    return Summarizer(llm, "Me", timezone="Europe/Berlin", **opts)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(ingest_token=TOKEN, data_dir=tmp_path / "data", silence_stop_seconds=60,
                    max_session_seconds=600, session_idle_seconds=120, run_worker=False,
                    vad_backend="energy",
                    # FakeTranscriber returns one piece per clip: one clip per region here;
                    # packed windows are tested in test_clips.py.
                    whisper_window_seconds=0)


@pytest.fixture
def llm_log() -> list:
    return []


@pytest.fixture
def pipeline(settings: Settings, llm_log: list) -> Pipeline:
    store = Store(settings.db_path)
    llm = LLMClient("http://llm.test/v1", "test-model", transport=fake_llm_transport(log=llm_log))
    store.put_speaker("Me", ToneEmbedder().embed(tone(200, 2)), is_owner=True)
    diarizer = Diarizer(ToneEmbedder(), load_voices(store))
    return Pipeline(settings, store, EnergyVAD(), FakeTranscriber(), diarizer,
                    Summarizer(llm, "Me"))


@pytest.fixture
def client(pipeline: Pipeline):
    from fastapi.testclient import TestClient

    from athena.api import create_app

    with TestClient(create_app(pipeline)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c
