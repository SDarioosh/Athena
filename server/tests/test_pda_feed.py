"""The PC relay's T5 PDA feed and actions (firmware/recorder/tools/athena_relay) against the
real API, with the fictional conversations of scripts/seed_demo.py and the fake LLM."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from athena.pipeline import Pipeline
from athena.store import Segment

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware" / "recorder" / "tools"))
sys.path.insert(0, str(ROOT / "server" / "scripts"))

from athena_relay.pda_actions import ActionRunner  # noqa: E402
from athena_relay.pda_feed import FEED_MAX, build_feed, encode_feed  # noqa: E402
from athena_relay.uploader import UploadError, classify  # noqa: E402
from seed_demo import CONVERSATIONS, ME  # noqa: E402

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC).timestamp()  # 48 h back: maria-oct, tv-news


class ClientApi:
    """The relay's ServerApi over FastAPI's TestClient, with HttpApi's error rules."""

    def __init__(self, client) -> None:
        self.c = client

    def _check(self, method: str, path: str, r) -> dict[str, Any]:
        if r.status_code != 200:
            raise UploadError(classify(r.status_code), f"{method} {path}: {r.status_code}",
                              r.status_code)
        return r.json()

    def health(self) -> None:
        self._check("GET", "/health", self.c.get("/health"))

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._check("GET", path, self.c.get(path, params=params))

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._check("PATCH", path, self.c.patch(path, json=body))

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._check("POST", path, self.c.post(path, json=body or {}))


def seed(pipeline: Pipeline) -> None:
    """scripts/seed_demo.py's insert step, summarized by the fake LLM."""
    store = pipeline.store
    for key, start, device, lines in CONVERSATIONS:
        sid = f"demo-{key}"
        t0 = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
        segs = [Segment(t0 + 8 * i, t0 + 8 * i + 7, text, speaker, speaker == ME)
                for i, (speaker, text) in enumerate(lines)]
        with store.tx() as c:
            end = segs[-1].ended_at
            c.execute("INSERT INTO sessions (id, device, started_at, last_chunk_at,"
                      " last_speech_at, received_at, status, stop_reason)"
                      " VALUES (?, ?, ?, ?, ?, ?, 'processing', 'demo')",
                      (sid, device, t0, end, end, time.time()))
        store.replace_segments(sid, device, segs, [(t0, end + 1)])
        for cid in pipeline._assign_conversations(sid, device):
            assert pipeline.summarize_conversation(cid)
        store.set_session_status(sid, "done")


@pytest.fixture
def api(pipeline: Pipeline, client) -> ClientApi:
    seed(pipeline)
    assert client.post("/v1/digests/2026-10-17").status_code == 200
    return ClientApi(client)


def test_feed_from_the_real_api(api: ClientApi) -> None:
    feed = build_feed(api, now=NOW, tz_name="UTC")
    raw = encode_feed(feed)
    assert len(raw) <= FEED_MAX and json.loads(raw) == feed
    assert feed["briefing"]["date"] == "2026-10-17"  # no digest for "today" (10-18)
    assert feed["briefing"]["text"].startswith("A day of plans")
    convs = feed["conversations"]
    assert [c["at"] for c in convs] == ["2026-10-17T21:00:00Z", "2026-10-16T20:00:00Z"]
    assert all(c["title"] and c["summary"] and c["id"] for c in convs)
    assert feed["actions"] and all(isinstance(x["id"], int) for x in feed["actions"])
    assert {x["conv"] for x in feed["actions"]} >= {c["id"] for c in convs}
    assert feed["calendar"] == []


def test_actions_against_the_real_api(api: ClientApi, pipeline: Pipeline) -> None:
    runner = ActionRunner(api)
    feed = build_feed(api, now=NOW, tz_name="UTC")
    maria = feed["conversations"][-1]["id"]  # maria-oct, 2026-10-16
    item = next(x for x in feed["actions"] if x["conv"] == maria)
    raw = json.dumps({"t": "item_status", "id": item["id"], "status": "done"}).encode()
    assert runner.run(1, raw).result == 0
    assert runner.run(1, raw).result == 0  # a re-sent action (lost ack) is harmless
    assert item["id"] not in [x["id"] for x in build_feed(api, now=NOW,
                                                          tz_name="UTC")["actions"]]
    assert runner.run(2, b'{"t":"item_status","id":999999,"status":"done"}').result == 1

    conv_id = item["conv"]
    page = runner.run(3, json.dumps({"t": "fetch", "ref": f"transcript:{conv_id}"})
                      .encode()).detail
    assert page["segments"] and page["next"] is None
    assert {x["s"] for x in page["segments"]} == {"Maria", ME}
    assert any(x["me"] for x in page["segments"])
    conv = runner.run(4, json.dumps({"t": "fetch", "ref": f"conv:{conv_id}"}).encode()).detail
    assert conv["title"] and conv["actions"]
    assert runner.run(5, b'{"t":"fetch","ref":"conv:nope"}').result == 1
    assert runner.run(6, b'{"t":"digest_refresh","date":"2026-10-17"}').result == 0
    assert runner.run(7, b'{"t":"digest_refresh","date":"2020-01-01"}').result == 1
