#!/usr/bin/env python3
"""Side-by-side evaluation of knowledge frontends over the convo MCP server.

Uses the store seeded by scripts/seed_demo.py. Each question has the demo conversation(s)
that answer it and keywords the answer must contain. A frontend passes a question when
it called a convo tool, cited a link to an expected conversation, and included a keyword.

    python eval/bakeoff.py --frontend raw     # llama-server + MCP directly (baseline)
    python eval/bakeoff.py --frontend hermes  # `hermes -p convo` one-shot per question
    python eval/bakeoff.py --frontend odysseus  # Odysseus agent chat; env ODYSSEUS_URL,
        # ODYSSEUS_USER, ODYSSEUS_PASSWORD(_FILE) or ODYSSEUS_API_TOKEN, ODYSSEUS_PRESET,
        # ODYSSEUS_MODE (agent, or chat = the web UI default)

Results go to eval/results/<frontend>.jsonl (gitignored) as JSON lines. A full run replaces
the file; --only appends to it.

Each row has `elapsed` (seconds for the whole answer) and `first_text_s`: seconds from
sending the question to the first answer text the user would see (null when no answer
text arrived). For odysseus that is the first SSE event of /api/chat_stream carrying
visible text (a non-thinking `delta`, or a `final_response` with content, reasoning
blocks left out, as static/js/chat.js shows it); for raw the first streamed content
token of llama-server; for hermes the first stream-json text event, else its `result`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

TODAY = "2026-10-18"
# (question, expected demo keys or [] for "should find nothing", keywords any-of)
QUESTIONS: list[tuple[str, list[str], list[str]]] = [
    ("What did Alice say about the budget in September?", ["budget-sep"], ["15", "fifteen"]),
    ("What's the latest on the Q4 budget cut?", ["alice-1on1"], ["10", "ten"]),
    ("What did Tom say on October 3rd?", ["landlord"], ["1520", "1,520", "fifteen twenty"]),
    ("When is my next dentist appointment?", ["dentist"], ["21"]),
    ("What am I getting Mom for her birthday?", ["maria-sep"], ["photo album"]),
    ("Where and when is Mom's birthday dinner?", ["maria-oct"], ["Casa Lupo"]),
    ("What did Ben say in the standup on October 7th?", ["standup"], ["migration", "database"]),
    ("When was the client demo moved to?", ["standup"], ["23"]),
    ("What did Maria ask me to do last week?", ["maria-oct"], ["Rosa", "train", "station"]),
    ("How much will my rent be from January?", ["landlord"], ["1520", "1,520"]),
    ("What happened in my conversations on September 30?", ["dentist"], ["crown", "molar"]),
    ("Who is taking over Alice's vendor contracts?", ["alice-1on1"], ["you", "Me"]),
    ("Did anyone mention a marathon?", ["jonas-dinner"], ["Lisbon", "December"]),
    ("What did I promise Ben?", ["standup"], ["pull request", "review", "PR"]),
    ("What did Dr. Patel tell me to avoid?", ["dentist"], ["hard food", "nuts", "hard"]),
    ("What is Jonas allergic to?", ["jonas-dinner"], ["peanut"]),
    ("Which book did Jonas recommend?", ["jonas-dinner"], ["Overstory"]),
    ("When exactly is Alice moving to Berlin?", ["alice-1on1"], ["12", "twelfth"]),
    ("What do I need to do before October 31?", ["landlord"], ["sign", "lease", "renewal"]),
    ("What did Alice say about Mars?", [], []),
    # voice notes: the newest recordings are conversations, so this needs kind="note"
    ("What was my latest voice note about?", ["note-offsite"], ["cooking", "pasta"]),
    # Zoom meetings (MCP 0.16.0): a chat line, and a participant who never spoke
    ("What did Dana post in chat?", ["zoom-budget"], ["garden-budget-v3", "budget-v3"]),
    ("Who was in the budget meeting?", ["zoom-budget"], ["Rafael"]),
]

CONVO_TOOLS = {"search_transcripts", "search_conversations", "get_conversation",
               "get_transcript", "list_action_items", "update_action_item", "list_facts",
               "get_daily_digest", "list_people", "name_speaker", "list_unnamed_speakers",
               "reassign_line", "rename_speaker_everywhere", "retire_fact"}


def conv_ids(db: Path) -> dict[str, str]:
    import sqlite3

    need = {k for _, keys, _ in QUESTIONS for k in keys}
    ids: dict[str, str] = {}
    if db.exists():  # sqlite3.connect would create an empty file
        try:
            rows = sqlite3.connect(db).execute(
                "SELECT DISTINCT session_id, conversation_id FROM segments"
                " WHERE session_id LIKE 'demo-%'")
            ids = {sid.removeprefix("demo-"): cid for sid, cid in rows}
        except sqlite3.OperationalError:
            pass
    if missing := sorted(need - ids.keys()):
        raise SystemExit(f"{db}: demo conversations missing ({', '.join(missing)}); "
                         "run scripts/seed_demo.py with the same CONVO_DATA_DIR first")
    return ids


def score(answer: str, tools: list[str], expected: list[str], keywords: list[str],
          ids: dict[str, str]) -> dict:
    used = any(t.split("_", 2)[-1] in CONVO_TOOLS or t in CONVO_TOOLS
               or any(t.endswith(c) for c in CONVO_TOOLS) for t in tools)
    cited = sorted(set(re.findall(r"/view/([0-9a-f]{32})", answer)))
    if not expected:  # nothing should be found: no citations, says it couldn't find it
        ok_cite = not cited
        ok_kw = bool(re.search(r"\b(no|not|couldn't|could not|nothing|didn't|did not)\b",
                               answer, re.IGNORECASE))
    else:
        want = {ids[k] for k in expected}
        ok_cite = bool(want & set(cited))
        ok_kw = any(k.lower() in answer.lower() for k in keywords)
    return {"tool_used": used, "cited_correct": ok_cite, "keyword": ok_kw,
            "pass": used and ok_cite and ok_kw}


# ------------------------------------------------------------- first visible text
_THINK_TAG = r"<(?:think(?:ing)?|thought)(?:\s[^>]*)?>"
_THINK_BLOCK = re.compile(r"<(think(?:ing)?|thought)(?:\s[^>]*)?>.*?</\1>", re.I | re.S)


def visible_text(text: str) -> str:
    """The part of streamed model text a chat UI shows as the answer: closed reasoning
    blocks removed, an unclosed one cut off (it is still thinking)."""
    out = _THINK_BLOCK.sub("", text)
    out = re.split(_THINK_TAG, out, maxsplit=1, flags=re.I)[0]
    return re.sub(r"</(?:think(?:ing)?|thought)>", "", out, flags=re.I)


class FirstText:
    """Seconds from `start` to the first answer text a user would see."""

    def __init__(self, start: float | None = None):
        self.start = time.monotonic() if start is None else start
        self.at: float | None = None
        self._buf = ""

    def text(self, chunk: str, now: float | None = None) -> None:
        """Answer text streamed in pieces (raw llama-server content tokens)."""
        if self.at is not None or not chunk:
            return
        self._buf += chunk
        if visible_text(self._buf).strip():
            self.at = time.monotonic() if now is None else now

    def whole(self, text: str, now: float | None = None) -> None:
        """A complete answer text (a final event), not added to the streamed pieces."""
        if self.at is None and visible_text(str(text or "")).strip():
            self.at = time.monotonic() if now is None else now

    def odysseus_event(self, ev: object, now: float | None = None) -> None:
        """One /api/chat_stream SSE event, judged like static/js/chat.js does: a
        `final_response` with content, or a `delta` string that isn't `thinking`."""
        if self.at is not None or not isinstance(ev, dict):
            return
        status = ev.get("status")
        if isinstance(status, int) and status >= 400:  # error events aren't answer text
            return
        if ev.get("type") == "final_response":
            self.whole(str(ev.get("content") or ev.get("delta") or ""), now)
        elif isinstance(ev.get("delta"), str) and not ev.get("thinking"):
            self.text(ev["delta"], now)

    def hermes_event(self, ev: object, now: float | None = None) -> None:
        """One `hermes --format stream-json` event. Best effort (the event names besides
        `tool_use` and `result` are a guess): a text-like event, else the final result."""
        if self.at is not None or not isinstance(ev, dict):
            return
        kind = str(ev.get("type") or "")
        if kind in {"text", "text_delta", "delta", "content", "assistant", "message",
                    "assistant_delta", "message_delta"}:
            for key in ("delta", "text", "content"):
                if isinstance(ev.get(key), str):
                    self.text(ev[key], now)
                    return
        elif kind == "result":
            self.whole(str(ev.get("text") or ""), now)

    @property
    def seconds(self) -> float | None:
        return None if self.at is None else round(self.at - self.start, 2)


class ChatStream:
    """Builds the assistant message from llama-server's streamed chat completion (SSE
    `data:` lines, OpenAI format), so the raw loop sees the same message as without
    streaming, and reports content tokens to a FirstText."""

    def __init__(self, first: FirstText | None = None):
        self.first = first
        self.content = ""
        self.calls: dict[int, dict] = {}
        self.done = False

    def feed(self, line: str, now: float | None = None) -> None:
        line = line.strip()
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                self.content += piece
                if self.first is not None:
                    self.first.text(piece, now)
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(int(tc.get("index", len(self.calls))), {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""

    def message(self) -> dict:
        m: dict = {"role": "assistant", "content": self.content}
        if self.calls:
            m["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return m


def median(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, int | float)]
    return round(statistics.median(vals), 1) if vals else None


def summary_line(frontend: str, rows: list[dict]) -> str:
    """`raw: 12/20 passed, median first text 3.1s, median total 9.8s`."""
    passed = sum(1 for r in rows if r.get("pass"))
    first = median([r.get("first_text_s") for r in rows])
    total = median([r.get("elapsed") for r in rows])
    no_text = sum(1 for r in rows if r.get("first_text_s") is None)

    def fmt(v):
        return "n/a" if v is None else f"{v:.1f}s"

    extra = f" ({no_text} without answer text)" if no_text else ""
    return (f"{frontend}: {passed}/{len(rows)} passed, median first text {fmt(first)}"
            f"{extra}, median total {fmt(total)}")


# ------------------------------------------------------------------ frontends
def run_hermes(question: str, profile: str, toolsets: str,
               first: FirstText | None = None) -> tuple[str, list[str]]:
    cmd = ["hermes", "-p", profile, "chat", "--oneshot", "-q", question, "-t", toolsets,
           "--max-turns", "10", "--format", "stream-json", "--yolo"]
    first = first or FirstText()
    # Read line by line (not subprocess.run) to see when the first text event arrives.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(900, proc.kill)  # same limit as before
    timer.start()
    answer, tools, out = "", [], []
    try:
        for line in proc.stdout:
            out.append(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            first.hermes_event(ev)
            if ev.get("type") == "tool_use":
                tools.append(str(ev.get("name", "")).removeprefix("mcp__convo__"))
            elif ev.get("type") == "result":
                answer = ev.get("text") or ""
        proc.wait()
    finally:
        timer.cancel()
    if not answer:
        answer = "".join(out)[-4000:]
    return str(answer), tools


_ODY: dict = {}


def _odysseus_client() -> httpx.Client:
    """Logged-in client for an Odysseus instance (env ODYSSEUS_URL/_USER/_PASSWORD[_FILE]
    or ODYSSEUS_API_TOKEN)."""
    if "client" in _ODY:
        return _ODY["client"]
    url = os.environ.get("ODYSSEUS_URL", "http://127.0.0.1:7010")
    token = os.environ.get("ODYSSEUS_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    c = httpx.Client(base_url=url, headers=headers, timeout=60)
    if not token:
        pw = os.environ.get("ODYSSEUS_PASSWORD")
        if not pw:
            pw_file = os.environ.get("ODYSSEUS_PASSWORD_FILE")
            if not pw_file:
                raise SystemExit("set ODYSSEUS_API_TOKEN, ODYSSEUS_PASSWORD or "
                                 "ODYSSEUS_PASSWORD_FILE for --frontend odysseus")
            pw = Path(pw_file).read_text().strip()
        r = c.post("/api/auth/login", json={"username": os.environ.get("ODYSSEUS_USER", "admin"),
                                            "password": pw, "remember": True})
        r.raise_for_status()
    _ODY["client"] = c
    return c


def _bare_tool(name: str) -> str:
    # mcp__<server-id>__search_transcripts -> search_transcripts
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def run_odysseus(question: str, first: FirstText | None = None) -> tuple[str, list[str]]:
    """One fresh Odysseus chat per question over /api/chat_stream (SSE).

    Agent mode by default; ODYSSEUS_MODE=chat sends what the web UI sends with the agent
    toggle off, so the eval also covers Odysseus switching the turn to agent mode itself.

    Tool-approval prompts ("Allow this task to continue?") are answered the way a user
    clicking "Allow for this task" would (ODYSSEUS_APPROVAL=approve_task|approve|deny).

    `first` (a FirstText started when the question is sent) records the first event
    carrying answer text the user would see.
    """
    first = first or FirstText()
    c = _odysseus_client()
    dc = c.get("/api/default-chat").json()
    sid = c.post("/api/session", data={"name": f"bakeoff: {question[:60]}",
                                       "endpoint_id": dc["endpoint_id"],
                                       "model": dc["model"]}).json()["id"]
    base = {"session": sid, "mode": os.environ.get("ODYSSEUS_MODE", "agent"),
            "allow_web_search": "false", "allow_bash": "false",
            "selected_endpoint_id": dc["endpoint_id"],
            "selected_model": dc["model"]}
    if os.environ.get("ODYSSEUS_PRESET", "custom"):
        base["preset_id"] = os.environ.get("ODYSSEUS_PRESET", "custom")
    form = {**base, "message": question}
    tools: list[str] = []
    approvals = 0
    for _ in range(6):
        pending = None
        with c.stream("POST", "/api/chat_stream", data=form, timeout=900) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                first.odysseus_event(ev)
                # Count executed calls only: blocked ones ("outside server request
                # authority") and approval placeholders never reached the MCP server.
                if (ev.get("type") == "tool_output" and ev.get("execution_attempted") is not False
                        and not ev.get("blocked")):
                    tools.append(_bare_tool(str(ev.get("tool", ""))))
                elif ev.get("type") == "ask_user":
                    data = ev.get("data") or {}
                    if data.get("kind") == "tool_approval":
                        pending = data.get("approval_id")
        if not pending or approvals >= 5:
            break
        approvals += 1
        form = {**base, "message": "", "tool_approval_id": pending,
                "tool_approval_decision": os.environ.get("ODYSSEUS_APPROVAL", "approve_task")}
    msgs = c.get(f"/api/session/{sid}/export", params={"fmt": "json"}).json()["messages"]
    answer = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant"), "")
    if approvals:
        print(f"  (odysseus: {approvals} tool approval(s) auto-accepted)", file=sys.stderr)
    return str(answer or ""), tools


async def _raw_session(questions, env):
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(command=str(Path(sys.executable).parent / "convo-server"),
                                   args=["mcp"], env=env)
    llm = os.environ.get("BAKEOFF_LLM", "http://127.0.0.1:8082/v1/chat/completions")
    async with Client(params) as client:
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.input_schema}}
                 for t in (await client.list_tools()).tools]
        system = (f"You answer questions about the user's recorded conversations. Today is "
                  f"{TODAY}. {client.instructions or ''}")
        for q in questions:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": q}]
            used = []
            answer = ""
            first = FirstText()
            async with httpx.AsyncClient(timeout=600) as http:
                for _ in range(8):
                    # Streamed, only to time the first content token; the message is
                    # put together again, so scoring sees what it saw unstreamed.
                    stream = ChatStream(first)
                    body = {"model": os.environ.get("BAKEOFF_MODEL", "qwen3.5-35b"),
                            "messages": msgs, "tools": tools, "temperature": 0.2,
                            "chat_template_kwargs": {"enable_thinking": False}}
                    m = None
                    async with http.stream("POST", llm, json={**body, "stream": True}) as resp:
                        if resp.status_code < 400:
                            if "json" in resp.headers.get("content-type", ""):  # not streamed
                                m = json.loads(await resp.aread())["choices"][0]["message"]
                                first.whole(m.get("content") or "")
                            else:
                                async for line in resp.aiter_lines():
                                    stream.feed(line)
                                m = stream.message()
                    if m is None:  # a server that can't stream with tools: as before
                        m = (await http.post(llm, json=body)).json()["choices"][0]["message"]
                        first.whole(m.get("content") or "")
                    msgs.append(m)
                    if not m.get("tool_calls"):
                        answer = m.get("content") or ""
                        break
                    for tc in m["tool_calls"]:
                        used.append(tc["function"]["name"])
                        fn = tc["function"]
                        call_args = json.loads(fn["arguments"] or "{}")
                        res = await client.call_tool(fn["name"], call_args)
                        text = "\n".join(getattr(c, "text", "") for c in res.content)
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
            yield q, answer, used, first.seconds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", choices=["raw", "hermes", "odysseus"], required=True)
    ap.add_argument("--profile", default="convo")
    ap.add_argument("--toolsets", default="convo")
    ap.add_argument("--only", type=int, nargs="*", help="question numbers (1-based)")
    args = ap.parse_args()

    from athena.config import env_setting, resolve_db_path

    data_dir = env_setting("DATA_DIR")
    if data_dir is None:
        raise SystemExit("set CONVO_DATA_DIR (or ATHENA_DATA_DIR) to the seeded data dir")
    ids = conv_ids(resolve_db_path(Path(data_dir)))
    todo = [(i, item[0]) for i, item in enumerate(QUESTIONS, 1) if not args.only or i in args.only]
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{args.frontend}.jsonl"
    if not args.only:  # a full run replaces the previous one; --only adds to it
        out.write_text("")

    rows: list[dict] = []

    def record(i, q, answer, tools, elapsed, first_text_s=None):
        _, expected, kws = QUESTIONS[i - 1]
        s = score(answer, tools, expected, kws, ids)
        row = {"n": i, "q": q, "frontend": args.frontend, "elapsed": round(elapsed, 1),
               "first_text_s": first_text_s, "tools": tools, **s, "answer": answer}
        rows.append(row)
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        mark = "PASS" if s["pass"] else "FAIL"
        ft = "  n/a" if first_text_s is None else f"{first_text_s:5.1f}s"
        print(f"{mark} #{i:2d} {elapsed:5.1f}s first={ft} tools={tools} {s} :: {q}",
              flush=True)
        return s["pass"]

    passed = 0
    if args.frontend == "hermes":
        for i, q in todo:
            t = time.time()
            first = FirstText()
            answer, tools = run_hermes(q, args.profile, args.toolsets, first)
            passed += record(i, q, answer, tools, time.time() - t, first.seconds)
    elif args.frontend == "odysseus":
        for i, q in todo:
            t = time.time()
            first = FirstText()
            try:
                answer, tools = run_odysseus(q, first)
            except httpx.HTTPError as e:
                answer, tools = f"ERROR: {type(e).__name__}: {e}", []
            passed += record(i, q, answer, tools, time.time() - t, first.seconds)
    else:
        # PYTHONPATH too, so a run from another checkout serves that checkout's code
        env = {k: v for k, v in os.environ.items()
               if k.startswith(("CONVO_", "ATHENA_")) or k == "PYTHONPATH"}
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")

        async def go():
            n = 0
            t = time.time()
            gen = _raw_session([q for _, q in todo], env)
            idx = iter(todo)
            async for q, answer, used, first_text_s in gen:
                i, _ = next(idx)
                n += record(i, q, answer, used, time.time() - t, first_text_s)
                t = time.time()
            return n

        passed = asyncio.run(go())
    print(f"\n{args.frontend}: {passed}/{len(todo)} passed")
    print(summary_line(args.frontend, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
