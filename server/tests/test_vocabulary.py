"""Words whisper is nudged toward, set in the hub's settings (athena/vocabulary.py)."""

from __future__ import annotations

import httpx
import numpy as np
import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient

from athena import vocabulary
from athena.api import create_app
from athena.ingest_relay import add_ingest_routes
from athena.mcp_server import BearerAuth, build_mcp
from athena.store import Store

MCP_TOKEN = "mcp-token-abcdefghijkl"


def api(pipeline) -> TestClient:
    c = TestClient(create_app(pipeline))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_csv_text_is_cleaned():
    typed = ' RokTalk, Meshtastic,,Rokland. ;"LoRa"\nmeshtastic ,  T-Watch  Ultra'
    assert vocabulary.parse(typed) == [
        "RokTalk", "Meshtastic", "Rokland", "LoRa", "T-Watch Ultra"]
    assert vocabulary.parse(["RokTalk, Meshtastic", "Rokland"]) == [
        "RokTalk", "Meshtastic", "Rokland"]
    assert vocabulary.parse("") == [] and vocabulary.parse(None) == []


@pytest.mark.parametrize("bad", [
    ", ".join(f"w{i}" for i in range(vocabulary.MAX_WORDS + 1)),
    "x" * (vocabulary.MAX_WORD + 1),
    "a\x00b",
    "a, " * 2000,
])
def test_limits(bad):
    with pytest.raises(ValueError):
        vocabulary.parse(bad)


def test_words_go_into_whisper_prompt_between_env_prompt_and_names(pipeline):
    pipeline.settings.whisper_prompt = "Odysseus."
    pipeline.store.put_speaker("Kath", np.ones(16, np.float32), is_owner=False)
    pipeline.store.put_speaker("Rokland", np.ones(16, np.float32), is_owner=False)
    assert pipeline.whisper_prompt() == "Odysseus. Kath, Rokland."
    vocabulary.set_words(pipeline, "RokTalk, Meshtastic, Rokland")
    # a name already in the list isn't said twice
    assert pipeline.whisper_prompt() == "Odysseus. RokTalk, Meshtastic, Rokland. Kath."
    vocabulary.set_words(pipeline, "")
    assert pipeline.store.get_setting(vocabulary.KEY) is None
    assert pipeline.whisper_prompt() == "Odysseus. Kath, Rokland."


def test_api_get_and_put_survive_a_restart(pipeline, settings):
    c = api(pipeline)
    assert c.get("/v1/settings/vocabulary").json() == {
        "words": [], "text": "", "max_words": vocabulary.MAX_WORDS, "env_prompt": "",
        "prompt": ""}
    r = c.put("/v1/settings/vocabulary", json={"words": "RokTalk, Meshtastic, Rokland"})
    assert r.status_code == 200
    assert r.json()["words"] == ["RokTalk", "Meshtastic", "Rokland"]
    assert r.json()["text"] == "RokTalk, Meshtastic, Rokland"
    assert r.json()["prompt"] == "RokTalk, Meshtastic, Rokland."
    assert vocabulary.words(Store(settings.db_path)) == ["RokTalk", "Meshtastic", "Rokland"]
    r = c.put("/v1/settings/vocabulary", json={"words": "x" * 100})
    assert r.status_code == 422 and "longer than" in r.json()["detail"]
    assert c.put("/v1/settings/vocabulary", json={"words": None}).json()["words"] == []
    assert TestClient(create_app(pipeline)).get("/v1/settings/vocabulary").status_code == 401


def test_recordings_use_the_words(pipeline):
    from test_recognition import ingest, tone

    vocabulary.set_words(pipeline, "RokTalk, Meshtastic")
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.transcriber.prompts == ["RokTalk, Meshtastic."]


def test_reachable_through_the_mcp_http_app(pipeline):
    mcp = build_mcp(pipeline.store, pipeline.settings)
    add_ingest_routes(mcp, pipeline.settings,
                      transport=httpx.ASGITransport(app=create_app(pipeline)))
    relay = TestClient(BearerAuth(mcp.streamable_http_app(), MCP_TOKEN))
    q = f"?access_token={MCP_TOKEN}"
    assert relay.get("/settings/vocabulary").status_code == 401
    r = relay.put(f"/settings/vocabulary{q}", json={"words": "RokTalk, Meshtastic"})
    assert r.status_code == 200 and r.json()["words"] == ["RokTalk", "Meshtastic"]
    assert relay.get(f"/settings/vocabulary{q}").json()["text"] == "RokTalk, Meshtastic"
    assert relay.put(f"/settings/vocabulary{q}", json={"words": "a\x01"}).status_code == 422
