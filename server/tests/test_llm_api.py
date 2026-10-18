"""LLM client, whisper output parsing and the read API."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from conftest import silence, tone, wav

from convo_server.llm import ConversationSummary, LLMClient, LLMError, _strip_fences
from convo_server.transcribe import parse_cli_json, parse_verbose_json

T0 = 1_760_000_000.0


def test_structured_retries_then_fails():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"nope\": 1}"}}]})

    llm = LLMClient("http://x/v1", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        llm.structured("s", "u", ConversationSummary, retries=1)
    assert len(calls) == 2


def test_structured_http_error():
    llm = LLMClient("http://x/v1", "m",
                    transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(LLMError, match="500"):
        llm.structured("s", "u", ConversationSummary)


def test_strip_fences():
    assert _strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_parse_whisper_outputs():
    assert parse_verbose_json({"segments": [{"start": 0, "end": 1.5, "text": " Hi "},
                                            {"start": 2, "end": 3, "text": "[BLANK_AUDIO]"}]}
                              )[0].text == "Hi"
    assert parse_verbose_json({"text": "  "}) == []
    pieces = parse_cli_json({"transcription": [{"offsets": {"from": 500, "to": 1500},
                                                "text": " hello"}]})
    assert (pieces[0].start, pieces[0].end, pieces[0].text) == (0.5, 1.5, "hello")


def _process_one(client, pipeline):
    client.post("/v1/ingest/chunk", files={"audio": ("c.wav", wav(np.concatenate(
        [tone(200, 5), silence(25)])))}, data={"device": "omi", "session_id": "s1",
        "sequence": "0", "timestamp": str(T0), "codec": "wav"})
    client.post("/v1/sessions/s1/end")
    pipeline.process_pending()
    return client.get("/v1/conversations").json()["conversations"][0]


def test_read_api(client, pipeline):
    conv = _process_one(client, pipeline)
    assert conv["title"] == "Planning the trip"
    assert conv["started_at"].startswith("2025-10-09")
    detail = client.get(f"/v1/conversations/{conv['id']}").json()
    assert detail["action_items"][0]["source_url"].endswith("#t=1")
    tr = client.get(detail["transcript_url"]).json()
    assert tr["segments"][0]["speaker"] == "Me" and tr["segments"][0]["is_owner"] is True

    res = client.get("/v1/search", params={"q": "Lisbon"}).json()["results"]
    assert res[0]["conversation_id"] == conv["id"]
    assert client.get("/v1/conversations", params={"person": "alice"}).json()["conversations"]
    assert not client.get("/v1/conversations",
                          params={"person": "bob"}).json()["conversations"]

    items = client.get("/v1/action-items").json()["action_items"]
    assert items[0]["conversation_title"] == "Planning the trip"
    assert client.patch(f"/v1/action-items/{items[0]['id']}",
                        json={"status": "done"}).json() == {"ok": True}
    assert client.get("/v1/action-items").json()["action_items"] == []
    assert client.patch(f"/v1/action-items/{items[0]['id']}",
                        json={"status": "weird"}).status_code == 400


def test_digest_api(client, pipeline):
    _process_one(client, pipeline)
    assert client.post("/v1/digests/not-a-date").status_code == 400
    assert client.get("/v1/digests/2025-10-09").status_code == 404
    assert client.post("/v1/digests/2025-10-09").json()["headline"] == "A day of plans"
    assert client.get("/v1/digests/2025-10-09").status_code == 200


def test_enroll_speaker(client):
    r = client.post("/v1/speakers/enroll", files={"audio": ("v.wav", wav(tone(900, 12)))},
                    data={"name": "Bob", "codec": "wav"})
    assert r.status_code == 200, r.text
    names = {s["name"] for s in client.get("/v1/speakers").json()["speakers"]}
    assert names == {"Bob"}  # fixture's in-memory owner voice isn't persisted
    too_short = client.post("/v1/speakers/enroll", files={"audio": ("v.wav", wav(tone(9, 1)))},
                            data={"name": "X", "codec": "wav"})
    assert too_short.status_code == 400
    assert json.loads(too_short.text)["detail"]


def test_summary_filters_placeholder_people_and_absent_owner():
    from conftest import fake_llm_transport

    from convo_server.llm import Summarizer
    from convo_server.store import Segment

    summary = {"title": "t", "summary": "s", "key_points": [], "action_items": [],
               "people": ["Speaker 1", "Me", "Alice", "alice", "Unknown"], "topics": [],
               "facts": []}
    llm = LLMClient("http://x/v1", "m", transport=fake_llm_transport(summary=summary))
    out = Summarizer(llm, "Me").summarize([Segment(0, 1, "hi", "Speaker 1")])
    assert out["people"] == ["Alice"]
    out = Summarizer(llm, "Me").summarize([Segment(0, 1, "hi", "Me", is_owner=True)])
    assert out["people"] == ["Me", "Alice"]
