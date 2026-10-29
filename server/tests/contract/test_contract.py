"""Contract snapshots: what the hub and the phone relay read from this module, byte for byte.

Every MCP tool (text and JSON formats), the MCP tool list (names, argument schemas,
annotations), the relay's HTTP read routes on the MCP app, the REST reads the T5 sync
uses and one ``/view`` page are compared with the files in ``snapshots/``. The store is
fixed and fictional (``demo_store.py``); each case that writes runs on a copy of its own,
so write tools and test order can't change another case's output.

A difference fails with a unified diff. When a change of output is intended (and allowed:
the contract rules in AGENTS.md), rewrite the files and review their diff:

    UPDATE_SNAPSHOTS=1 pytest tests/contract
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from demo_store import (
    INGEST_TOKEN,
    MCP_TOKEN,
    build,
    frozen,
    make_pipeline,
    settings_for,
)
from fastapi.testclient import TestClient

from athena.api import create_app
from athena.db.store import Store
from athena.ingest_relay import add_ingest_routes
from athena.links import sign
from athena.mcp_server import BearerAuth, build_mcp
from athena.pipeline import Pipeline

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
REGENERATE = "UPDATE_SNAPSHOTS=1 pytest tests/contract"


def cid(n: int) -> str:
    """The demo store's n-th conversation id (ids are fixed while it is built)."""
    return f"{0xC0DE0000 + n:032x}"


BUDGET_SEP, JONAS, NOTE_BIKE, ZOOM_BUDGET = cid(1), cid(7), cid(8), cid(9)
ALICE_1ON1, TV_NEWS, BAKERY, WRITTEN = cid(10), cid(13), cid(14), cid(16)
MISSING = "ffffffffffffffffffffffffffffffff"
ALICE_FACT, JONAS_FACT = 1, 9  # fact ids: Alice moves to Berlin, Jonas's peanut allergy


# ------------------------------------------------------------------ snapshots
def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def check(name: str, text: str) -> None:
    """Compare ``text`` with ``snapshots/<name>``; with UPDATE_SNAPSHOTS=1 write it."""
    path = SNAPSHOTS / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"missing snapshot {name}; create it with: {REGENERATE}")
    want = path.read_text(encoding="utf-8")
    if want != text:
        diff = "".join(difflib.unified_diff(
            want.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=f"snapshots/{name}", tofile="now"))
        pytest.fail(f"contract output changed: {name}\n{diff}\n"
                    f"If the change is intended and allowed, regenerate with: {REGENERATE}",
                    pytrace=False)


def check_result(name: str, out: str) -> None:
    """A tool's result: JSON (format="json") as ``mcp_<name>.json``, text as .txt."""
    value: Any = None
    if out.startswith(("{", "[")):
        try:
            value = json.loads(out)
        except ValueError:
            value = None
    if value is None:
        check(f"mcp_{name}.txt", out + "\n")
    else:
        check(f"mcp_{name}.json", _json_text(value))


# ------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo store, built once per run."""
    data = tmp_path_factory.mktemp("contract-template") / "data"
    build(data)
    return data / "convo.db"


def _copy(template: Path, data_dir: Path) -> Pipeline:
    """A pipeline over a copy of the demo store in ``data_dir``."""
    settings = settings_for(data_dir)
    settings.db_path.parent.mkdir(parents=True)
    src, dst = sqlite3.connect(template), sqlite3.connect(settings.db_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return make_pipeline(settings, Store(settings.db_path))


@pytest.fixture(scope="module")
def shared(template: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pipeline]:
    """One copy of the demo store for the cases that only read, with the clock fixed
    while this module runs."""
    with pytest.MonkeyPatch.context() as mp:
        frozen(mp)
        pipeline = _copy(template, tmp_path_factory.mktemp("contract-shared") / "data")
        yield pipeline
        pipeline.store.close()


@pytest.fixture
def fresh(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
          ) -> Iterator[Pipeline]:
    """A copy of the demo store of its own, for a case that writes."""
    frozen(monkeypatch)
    pipeline = _copy(template, tmp_path / "data")
    yield pipeline
    pipeline.store.close()


@pytest.fixture(scope="module")
def shared_mcp(shared: Pipeline) -> Any:
    return build_mcp(shared.store, shared.settings)


def call(mcp: Any, tool: str, args: dict[str, Any]) -> str:
    from mcp import Client

    async def go() -> str:
        async with Client(mcp) as client:
            res = await client.call_tool(tool, args)
            return "\n".join(c.text for c in res.content if hasattr(c, "text"))

    return asyncio.run(go())


# ------------------------------------------------------------------ MCP tools
# (snapshot name, tool, arguments); a JSON result is saved as .json, the rest as .txt.
TOOL_CASES: list[tuple[str, str, dict[str, Any]]] = [
    # search_transcripts
    ("search_transcripts__speaker", "search_transcripts", {"speaker": "Alice"}),
    ("search_transcripts__query", "search_transcripts", {"query": "peanuts"}),
    ("search_transcripts__dates", "search_transcripts",
     {"since": "2026-10-13", "until": "2026-10-13"}),
    ("search_transcripts__unknown_speaker", "search_transcripts", {"speaker": "Zed"}),
    ("search_transcripts__bad_date", "search_transcripts", {"since": "last tuesday"}),
    # search_conversations
    ("search_conversations__default", "search_conversations", {}),
    ("search_conversations__query", "search_conversations", {"query": "budget"}),
    ("search_conversations__person", "search_conversations", {"person": "Maria"}),
    ("search_conversations__dates", "search_conversations",
     {"since": "2026-09-01", "until": "2026-09-30", "limit": 50}),
    ("search_conversations__kind_note", "search_conversations", {"kind": "note"}),
    ("search_conversations__kind_voice", "search_conversations", {"kind": "voice"}),
    ("search_conversations__kind_written", "search_conversations", {"kind": "written"}),
    ("search_conversations__kind_conversation", "search_conversations",
     {"kind": "conversation", "limit": 50}),
    ("search_conversations__kind_meeting", "search_conversations", {"kind": "meeting"}),
    ("search_conversations__kind_in_person", "search_conversations",
     {"kind": "in_person", "limit": 50}),
    ("search_conversations__kind_memo", "search_conversations", {"kind": "memo"}),
    ("search_conversations__kind_zoom_query", "search_conversations",
     {"kind": "zoom", "query": "newsletter"}),
    ("search_conversations__bad_kind", "search_conversations", {"kind": "podcast"}),
    ("search_conversations__nothing", "search_conversations", {"query": "zeppelin"}),
    # get_conversation
    ("get_conversation__budget_sep", "get_conversation", {"conversation_id": BUDGET_SEP}),
    ("get_conversation__alice_1on1", "get_conversation", {"conversation_id": ALICE_1ON1}),
    ("get_conversation__meeting", "get_conversation", {"conversation_id": ZOOM_BUDGET}),
    ("get_conversation__voice_note", "get_conversation", {"conversation_id": NOTE_BIKE}),
    ("get_conversation__written", "get_conversation", {"conversation_id": WRITTEN}),
    ("get_conversation__discarded", "get_conversation", {"conversation_id": TV_NEWS}),
    ("get_conversation__missing", "get_conversation", {"conversation_id": MISSING}),
    # get_transcript
    ("get_transcript__jonas", "get_transcript", {"conversation_id": JONAS}),
    ("get_transcript__meeting_chat", "get_transcript", {"conversation_id": ZOOM_BUDGET}),
    ("get_transcript__window", "get_transcript",
     {"conversation_id": BUDGET_SEP, "start_seconds": 8, "end_seconds": 40, "max_lines": 2}),
    ("get_transcript__missing", "get_transcript", {"conversation_id": MISSING}),
    # list_action_items
    ("list_action_items__text", "list_action_items", {}),
    ("list_action_items__json", "list_action_items", {"format": "json"}),
    ("list_action_items__all_json", "list_action_items", {"status": "all", "format": "json"}),
    ("list_action_items__person", "list_action_items", {"person": "Alice"}),
    ("list_action_items__person_json", "list_action_items",
     {"person": "Maria", "format": "json"}),
    ("list_action_items__dates", "list_action_items",
     {"since": "2026-10-01", "until": "2026-10-10"}),
    ("list_action_items__done", "list_action_items", {"status": "done"}),
    ("list_action_items__bad_status", "list_action_items", {"status": "later"}),
    ("list_action_items__bad_format", "list_action_items", {"format": "xml"}),
    # list_facts
    ("list_facts__text", "list_facts", {}),
    ("list_facts__json", "list_facts", {"format": "json"}),
    ("list_facts__text_confirmed", "list_facts", {"confirmed": True}),
    ("list_facts__json_confirmed", "list_facts", {"confirmed": True, "format": "json"}),
    ("list_facts__subject", "list_facts", {"subject": "Jonas"}),
    ("list_facts__query_json", "list_facts", {"query": "budget", "format": "json"}),
    ("list_facts__limit", "list_facts", {"limit": 2}),
    ("list_facts__bad_format", "list_facts", {"format": "yaml"}),
    # get_daily_digest
    ("get_daily_digest__2026-10-14", "get_daily_digest", {"date": "2026-10-14"}),
    ("get_daily_digest__2026-09-15", "get_daily_digest", {"date": "2026-09-15"}),
    ("get_daily_digest__missing", "get_daily_digest", {"date": "2026-01-01"}),
    # list_people
    ("list_people__text", "list_people", {}),
    ("list_people__json", "list_people", {"format": "json"}),
    ("list_people__bad_format", "list_people", {"format": "csv"}),
    # list_unnamed_speakers
    ("list_unnamed_speakers__default", "list_unnamed_speakers", {}),
    ("list_unnamed_speakers__limit", "list_unnamed_speakers", {"limit": 1}),
    # write tools: each on its own fresh copy of the store
    ("update_action_item__done", "update_action_item", {"item_id": 1, "status": "done"}),
    ("update_action_item__fields", "update_action_item",
     {"item_id": 9, "kind": "reminder", "due_date": "2026-11-01", "due_time": "18:30",
      "list_name": "Reading", "title": "Read The Overstory"}),
    ("update_action_item__clear", "update_action_item",
     {"item_id": 3, "due_date": "none", "due_time": "none"}),
    ("update_action_item__bad_status", "update_action_item",
     {"item_id": 1, "status": "finished"}),
    ("update_action_item__bad_kind", "update_action_item", {"item_id": 1, "kind": "chore"}),
    ("update_action_item__missing", "update_action_item", {"item_id": 999, "status": "done"}),
    ("name_speaker__bakery", "name_speaker",
     {"conversation_id": BAKERY, "speaker_label": "Speaker 1", "name": "Greta"}),
    ("name_speaker__no_voice", "name_speaker",
     {"conversation_id": BAKERY, "speaker_label": "Speaker 1", "name": "Greta",
      "remember_voice": False}),
    ("name_speaker__unknown_label", "name_speaker",
     {"conversation_id": BAKERY, "speaker_label": "Speaker 7", "name": "Greta"}),
    ("name_speaker__empty_name", "name_speaker",
     {"conversation_id": BAKERY, "speaker_label": "Speaker 1", "name": " "}),
    ("rename_speaker_everywhere__rename", "rename_speaker_everywhere",
     {"old_name": "Jonas", "new_name": "Jonah"}),
    ("rename_speaker_everywhere__merge", "rename_speaker_everywhere",
     {"old_name": "Ben", "new_name": "Alice"}),
    ("rename_speaker_everywhere__unnamed", "rename_speaker_everywhere",
     {"old_name": "Speaker 1", "new_name": "Greta"}),
    ("rename_speaker_everywhere__to_label", "rename_speaker_everywhere",
     {"old_name": "Jonas", "new_name": "Speaker 3"}),
    ("rename_speaker_everywhere__missing", "rename_speaker_everywhere",
     {"old_name": "Zed", "new_name": "Zoe"}),
    ("reassign_line__move", "reassign_line", {"line_id": 2, "name": "Alice"}),
    ("reassign_line__same", "reassign_line", {"line_id": 1, "name": "Alice"}),
    ("reassign_line__missing", "reassign_line", {"line_id": 9999, "name": "Alice"}),
    ("retire_fact__retire", "retire_fact", {"fact_id": JONAS_FACT, "reason": "wrong"}),
    ("retire_fact__missing", "retire_fact", {"fact_id": 999}),
    ("erase_fact__erase", "erase_fact", {"fact_id": ALICE_FACT, "reason": "private"}),
    ("erase_fact__missing", "erase_fact", {"fact_id": 999}),
]


WRITE_TOOLS = {"update_action_item", "name_speaker", "rename_speaker_everywhere",
               "reassign_line", "retire_fact", "erase_fact"}


@pytest.mark.parametrize("name,tool,args", TOOL_CASES, ids=[c[0] for c in TOOL_CASES])
def test_mcp_tool(request: pytest.FixtureRequest, name: str, tool: str,
                  args: dict[str, Any]) -> None:
    if tool in WRITE_TOOLS:
        pipeline = request.getfixturevalue("fresh")
        mcp = build_mcp(pipeline.store, pipeline.settings)
    else:
        mcp = request.getfixturevalue("shared_mcp")
    check_result(name, call(mcp, tool, args))


# Write tools again, then the reads the hub follows them with: what it sees afterwards.
AFTER_CASES: list[tuple[str, list[tuple[str, dict[str, Any]]], str, dict[str, Any]]] = [
    ("after_retire_fact__list_facts_json", [("retire_fact", {"fact_id": JONAS_FACT})],
     "list_facts", {"format": "json"}),
    ("after_erase_fact__get_transcript", [("erase_fact", {"fact_id": ALICE_FACT})],
     "get_transcript", {"conversation_id": BUDGET_SEP}),
    ("after_erase_fact__get_conversation", [("erase_fact", {"fact_id": ALICE_FACT})],
     "get_conversation", {"conversation_id": ALICE_1ON1}),
    ("after_rename_merge__list_people_json",
     [("rename_speaker_everywhere", {"old_name": "Ben", "new_name": "Alice"})],
     "list_people", {"format": "json"}),
    ("after_name_speaker__list_unnamed_speakers",
     [("name_speaker", {"conversation_id": BAKERY, "speaker_label": "Speaker 1",
                        "name": "Greta"})],
     "list_unnamed_speakers", {}),
    ("after_update_action_item__list_action_items_json",
     [("update_action_item", {"item_id": 1, "status": "done"}),
      ("update_action_item", {"item_id": 2, "status": "dismissed"})],
     "list_action_items", {"status": "all", "format": "json"}),
]


@pytest.mark.parametrize("name,writes,tool,args", AFTER_CASES,
                         ids=[c[0] for c in AFTER_CASES])
def test_mcp_after_write(fresh: Pipeline, name: str,
                         writes: list[tuple[str, dict[str, Any]]], tool: str,
                         args: dict[str, Any]) -> None:
    mcp = build_mcp(fresh.store, fresh.settings)
    for write, write_args in writes:
        call(mcp, write, write_args)
    check_result(name, call(mcp, tool, args))


def test_mcp_tool_list(shared_mcp: Any) -> None:
    """The tool list the hub gates on: names, argument schemas, annotations; and,
    separately, the descriptions and server instructions the model reads."""
    from mcp import Client

    async def info() -> dict[str, Any]:
        async with Client(shared_mcp) as client:
            tools = (await client.list_tools()).tools
            return {"server": client.server_info.model_dump(mode="json", exclude_none=True),
                    "instructions": client.instructions, "tools": tools}

    got = asyncio.run(info())
    tools = sorted(got["tools"], key=lambda t: t.name)
    check("mcp_tools.json", _json_text([
        {"name": t.name, "inputSchema": t.input_schema,
         "outputSchema": t.output_schema,
         "annotations": t.annotations.model_dump(mode="json", by_alias=True,
                                                 exclude_none=True)
         if t.annotations else None} for t in tools]))
    text = [f"server: {json.dumps(got['server'], sort_keys=True)}", "",
            "instructions:", got["instructions"] or "", ""]
    for t in tools:
        text += [f"## {t.name}", t.description or "", ""]
    check("mcp_tool_descriptions.txt", "\n".join(text))


# ----------------------------------------------------------------- HTTP reads
def _record(resp: httpx.Response, *, body: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"status": resp.status_code,
                           "content_type": resp.headers.get("content-type")}
    if body:
        try:
            out["body"] = resp.json()
        except ValueError:
            out["body"] = resp.text
    return out


@pytest.fixture(scope="module")
def relay(shared: Pipeline) -> TestClient:
    """The MCP HTTP app (behind BearerAuth) whose routes reach the real API in-process.
    Every route used here only reads, or refuses its input before it writes."""
    mcp = build_mcp(shared.store, shared.settings)
    add_ingest_routes(mcp, shared.settings,
                      transport=httpx.ASGITransport(app=create_app(shared)))
    client = TestClient(BearerAuth(mcp.streamable_http_app(), MCP_TOKEN))
    client.headers["Authorization"] = f"Bearer {MCP_TOKEN}"
    return client


@pytest.fixture(scope="module")
def api(shared: Pipeline) -> TestClient:
    client = TestClient(create_app(shared))
    client.headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
    return client


# The reads of READ_ROUTES (the phone relay's T5 sync and the hub's settings page), on
# the relay as paths without /v1, on the API with it; plus their error answers.
READS: list[tuple[str, str]] = [
    ("health", "/health"),
    ("conversations", "/conversations"),
    ("conversations__limit", "/conversations?limit=3"),
    ("conversations__since", "/conversations?since=2026-10-14T00:00:00Z"
                             "&until=2026-10-17T00:00:00Z"),
    ("conversations__person", "/conversations?person=Alice"),
    ("conversation", f"/conversations/{ALICE_1ON1}"),
    ("conversation__meeting", f"/conversations/{ZOOM_BUDGET}"),
    ("conversation__written", f"/conversations/{WRITTEN}"),
    ("conversation__missing", f"/conversations/{MISSING}"),
    ("transcript", f"/conversations/{JONAS}/transcript"),
    ("transcript__window", f"/conversations/{ZOOM_BUDGET}/transcript?start=10&end=30"),
    ("transcript__missing", f"/conversations/{MISSING}/transcript"),
    ("action_items", "/action-items"),
    ("action_items__all_notes", "/action-items?status=all&include_notes=true"),
    ("digest", "/digests/2026-10-14"),
    ("digest__missing", "/digests/2026-01-01"),
    ("settings_llm", "/settings/llm"),
    ("settings_vocabulary", "/settings/vocabulary"),
    ("speakers_enroll_next", "/speakers/enroll/next"),
]


@pytest.mark.parametrize("name,path", READS, ids=[r[0] for r in READS])
def test_relay_read(relay: TestClient, name: str, path: str) -> None:
    check(f"relay_{name}.json", _json_text(_record(relay.get(path))))


@pytest.mark.parametrize("name,path", READS, ids=[r[0] for r in READS])
def test_rest_read(api: TestClient, name: str, path: str) -> None:
    api_path = path if path == "/health" else "/v1" + path
    check(f"rest_{name}.json", _json_text(_record(api.get(api_path))))


# Relay routes outside READ_ROUTES that only read, and the validation answers of the
# rest (no bot is started and Zoom is never called: the ids and bodies are refused
# before that, or the route only reads the store).
RELAY_OTHER: list[tuple[str, str, str, dict[str, Any]]] = [
    ("meetings", "GET", "/meetings", {}),
    ("meetings__limit", "GET", "/meetings?limit=5", {}),
    ("meetings__bad_limit", "GET", "/meetings?limit=abc", {}),
    ("meeting__bad_id", "GET", "/meetings/not-a-meeting", {}),
    ("meeting__missing", "GET", f"/meetings/{MISSING}", {}),
    ("meeting_leave__bad_id", "DELETE", "/meetings/not-a-meeting", {}),
    ("meeting_join__not_json", "POST", "/meetings", {"content": b"url=x"}),
    ("zoom_signin", "GET", "/zoom/signin", {}),
    ("zoom_signin_start__not_configured", "POST", "/zoom/signin/start", {}),
    ("zoom_signin_finish__not_json", "POST", "/zoom/signin/finish", {"content": b"x"}),
    ("export__bad_format", "GET", f"/conversations/{JONAS}/export?format=pdf", {}),
    ("export__bad_include", "GET", f"/conversations/{JONAS}/export?include=everything", {}),
    ("export__missing", "GET", f"/conversations/{MISSING}/export", {}),
    ("action_item_patch__bad_status", "PATCH", "/action-items/1", {"json": {"status": "x"}}),
    ("action_item_patch__missing", "PATCH", "/action-items/999",
     {"json": {"status": "done"}}),
    ("digest_make__bad_date", "POST", "/digests/yesterday", {}),
    ("ingest_chunk__not_multipart", "POST", "/ingest/chunk", {"json": {"audio": "x"}}),
    ("ingest_text__not_json", "POST", "/ingest/text", {"content": b"x"}),
    ("session_end__missing", "POST", "/sessions/nope/end", {}),
]


@pytest.mark.parametrize("name,method,path,kwargs", RELAY_OTHER,
                         ids=[r[0] for r in RELAY_OTHER])
def test_relay_other(relay: TestClient, name: str, method: str, path: str,
                     kwargs: dict[str, Any]) -> None:
    check(f"relay_{name}.json", _json_text(_record(relay.request(method, path, **kwargs))))


def test_relay_needs_the_mcp_token(relay: TestClient) -> None:
    relay = TestClient(relay.app)  # no token
    out = {"no_token": _record(relay.get("/conversations")),
           "ingest_token": _record(relay.get(
               "/conversations", headers={"Authorization": f"Bearer {INGEST_TOKEN}"})),
           "query_token": _record(relay.get(f"/health?access_token={MCP_TOKEN}"))}
    check("relay_auth.json", _json_text(out))


@pytest.mark.parametrize("fmt,include", [("md", None), ("html", None),
                                         ("md", "summary,action_items,facts,transcript")])
def test_relay_export(relay: TestClient, fmt: str, include: str | None) -> None:
    """The hub's Share button: the file, and the headers the relay passes on."""
    query = f"?format={fmt}" + (f"&include={include}" if include else "")
    resp = relay.get(f"/conversations/{ZOOM_BUDGET}/export{query}")
    name = f"relay_export__{fmt}" + ("_all" if include else "")
    meta = _record(resp, body=False)
    meta["headers"] = {k: resp.headers.get(k) for k in
                       ("content-disposition", "cache-control", "x-content-type-options")}
    check(f"{name}.json", _json_text(meta))
    check(f"{name}.{fmt}", resp.text)


@pytest.mark.parametrize("name,conv", [("written", WRITTEN), ("meeting", ZOOM_BUDGET),
                                       ("conversation", BUDGET_SEP)])
def test_view_page(api: TestClient, shared: Pipeline, name: str, conv: str) -> None:
    api = TestClient(api.app)  # signed links need no token
    resp = api.get(f"/view/{conv}?sig={sign(shared.settings, conv)}")
    assert resp.status_code == 200
    check(f"view_{name}.html", resp.text)


def test_view_page_refuses_a_bad_signature(api: TestClient) -> None:
    api = TestClient(api.app)
    out = {"bad_sig": _record(api.get(f"/view/{BUDGET_SEP}?sig=0123")),
           "no_sig": _record(api.get(f"/view/{BUDGET_SEP}")),
           "missing": _record(api.get(f"/view/{MISSING}?sig=0123"))}
    check("view_refused.json", _json_text(out))


# ------------------------------------------------------------- hub fixtures
HUB_FIXTURES = ("mcp_list_facts__json.json", "mcp_list_action_items__json.json",
                "mcp_list_people__json.json")


def hub_fixture_paths() -> list[Path]:
    """The JSON snapshots the hub's tests copy as fixtures of this module's answers."""
    return [SNAPSHOTS / name for name in HUB_FIXTURES]


def test_hub_fixtures_exist() -> None:
    for path in hub_fixture_paths():
        assert path.is_file(), f"{path.name} is missing; run {REGENERATE}"
        json.loads(path.read_text(encoding="utf-8"))


def test_no_unused_snapshots() -> None:
    """Every file in snapshots/ belongs to a case above (a renamed case leaves none
    behind). Skipped with UPDATE_SNAPSHOTS=1, where stale files are listed instead."""
    names = {f"mcp_{c[0]}" for c in TOOL_CASES} | {f"mcp_{c[0]}" for c in AFTER_CASES}
    names |= {f"relay_{r[0]}" for r in READS} | {f"rest_{r[0]}" for r in READS}
    names |= {f"relay_{r[0]}" for r in RELAY_OTHER}
    names |= {"mcp_tools", "mcp_tool_descriptions", "relay_auth", "relay_export__md",
              "relay_export__html", "relay_export__md_all", "view_written", "view_meeting",
              "view_conversation", "view_refused"}
    stale = sorted(p.name for p in SNAPSHOTS.iterdir() if p.stem not in names)
    assert not stale, f"snapshots no case writes: {stale}; delete them"
