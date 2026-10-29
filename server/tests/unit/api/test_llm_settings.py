"""The summary model, changeable at run time (the hub's settings page)."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import TOKEN, FakeTranscriber, fake_llm_transport
from fastapi.testclient import TestClient

from athena import llm_settings
from athena.api import create_app
from athena.cli import build_pipeline
from athena.db.store import Store
from athena.ingest_relay import add_ingest_routes
from athena.mcp_server import BearerAuth, build_mcp
from athena.pipeline import Pipeline
from athena.summarize.client import LLMClient
from athena.summarize.summarizer import Summarizer
from athena.vad import EnergyVAD

MCP_TOKEN = "mcp-token-abcdefghijkl"
SERVED = ["qwen3.5-9b", "qwen3.6-35b"]


def llm_server(models: list[str] | None, chat_log: list) -> httpx.MockTransport:
    """Fake LLM server: ``GET /models`` lists ``models`` (None: no such route), chat
    completions answer like ``fake_llm_transport`` and record the requested model."""
    chat = fake_llm_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            if models is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"object": "list",
                                             "data": [{"id": m} for m in models]})
        chat_log.append(json.loads(request.content)["model"])
        return chat.handle_request(request)

    return httpx.MockTransport(handler)


def make_pipeline(settings, models=SERVED, chat_log=None) -> Pipeline:
    store = Store(settings.db_path)
    llm = LLMClient("http://llm.test/v1", settings.llm_model,
                    transport=llm_server(models, chat_log if chat_log is not None else []))
    return Pipeline(settings, store, EnergyVAD(), FakeTranscriber(), None, Summarizer(llm, "Me"))


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"llm_model": "qwen3.5-9b"})


def api(pipeline: Pipeline) -> TestClient:
    c = TestClient(create_app(pipeline))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_get_shows_model_default_and_what_the_server_lists(settings):
    c = api(make_pipeline(settings))
    assert c.get("/v1/settings/llm").json() == {
        "base_url": "http://llm.test/v1", "model": "qwen3.5-9b",
        "default_model": "qwen3.5-9b", "override": None, "available": SERVED}
    assert TestClient(create_app(make_pipeline(settings))).get(
        "/v1/settings/llm").status_code == 401


def test_put_switches_the_running_summarizer_and_survives_a_restart(settings):
    log: list = []
    p = make_pipeline(settings, chat_log=log)
    r = api(p).put("/v1/settings/llm", json={"model": "qwen3.6-35b"})
    assert r.status_code == 200
    assert r.json()["model"] == "qwen3.6-35b" and r.json()["override"] == "qwen3.6-35b"
    p.summarizer.llm.structured("s", "u", llm_settings_schema())
    assert log == ["qwen3.6-35b"]
    # a new process (convo-server serve after a restart) picks the stored model up
    llm = LLMClient("http://llm.test/v1", settings.llm_model)
    llm_settings.apply_stored(llm, Store(settings.db_path))
    assert llm.model == "qwen3.6-35b"


def test_build_pipeline_applies_the_stored_model(settings):
    store = Store(settings.db_path)
    store.set_setting(llm_settings.KEY, "qwen3.6-35b")
    p = build_pipeline(settings.model_copy(update={"diarization": False,
                                                   "vad_backend": "energy"}))
    assert p.summarizer.llm.model == "qwen3.6-35b"


def test_null_or_the_default_goes_back_to_the_env_model(settings):
    p = make_pipeline(settings)
    c = api(p)
    c.put("/v1/settings/llm", json={"model": "qwen3.6-35b"})
    for body in ({"model": None}, {}, {"model": "  "}, {"model": "qwen3.5-9b"}):
        c.put("/v1/settings/llm", json={"model": "qwen3.6-35b"})
        got = c.put("/v1/settings/llm", json=body).json()
        assert got["model"] == "qwen3.5-9b" and got["override"] is None, body
        assert p.store.get_setting(llm_settings.KEY) is None


def test_a_model_the_server_does_not_list_is_refused(settings):
    p = make_pipeline(settings)
    r = api(p).put("/v1/settings/llm", json={"model": "gpt-4o"})
    assert r.status_code == 422
    assert "no model 'gpt-4o'" in r.json()["detail"] and "qwen3.6-35b" in r.json()["detail"]
    assert p.summarizer.llm.model == "qwen3.5-9b"
    assert api(p).put("/v1/settings/llm", json={"model": "a\nb"}).status_code == 422
    assert api(p).put("/v1/settings/llm", json={"model": "x" * 201}).status_code == 422


def test_any_name_when_the_server_cannot_list(settings):
    p = make_pipeline(settings, models=None)
    c = api(p)
    assert c.get("/v1/settings/llm").json()["available"] is None
    assert c.put("/v1/settings/llm", json={"model": "qwen3.6-35b"}).json()["model"] == \
        "qwen3.6-35b"


def test_only_the_model_changes_never_the_server(settings):
    """A caller can't move summaries (and transcripts) to another host."""
    p = make_pipeline(settings)
    r = api(p).put("/v1/settings/llm", json={"model": "qwen3.6-35b",
                                             "base_url": "https://cloud.example/v1"})
    assert r.status_code == 200 and r.json()["base_url"] == "http://llm.test/v1"


def test_reachable_through_the_mcp_http_app(settings):
    p = make_pipeline(settings)
    mcp = build_mcp(p.store, p.settings)
    add_ingest_routes(mcp, p.settings, transport=httpx.ASGITransport(app=create_app(p)))
    relay = TestClient(BearerAuth(mcp.streamable_http_app(), MCP_TOKEN))
    q = f"?access_token={MCP_TOKEN}"
    assert relay.get("/settings/llm").status_code == 401
    assert relay.get(f"/settings/llm{q}").json()["available"] == SERVED
    r = relay.put(f"/settings/llm{q}", json={"model": "qwen3.6-35b"})
    assert r.status_code == 200 and p.summarizer.llm.model == "qwen3.6-35b"
    assert relay.put(f"/settings/llm{q}", json={"model": "nope"}).status_code == 422


def llm_settings_schema():
    from athena.summarize.schemas import Triage

    return Triage
