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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
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
]

CONVO_TOOLS = {"search_transcripts", "search_conversations", "get_conversation",
               "get_transcript", "list_action_items", "update_action_item", "list_facts",
               "get_daily_digest", "list_people", "name_speaker", "list_unnamed_speakers",
               "reassign_line", "rename_speaker_everywhere"}


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


# ------------------------------------------------------------------ frontends
def run_hermes(question: str, profile: str, toolsets: str) -> tuple[str, list[str]]:
    cmd = ["hermes", "-p", profile, "chat", "--oneshot", "-q", question, "-t", toolsets,
           "--max-turns", "10", "--format", "stream-json", "--yolo"]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=900)
    answer, tools = "", []
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "tool_use":
            tools.append(str(ev.get("name", "")).removeprefix("mcp__convo__"))
        elif ev.get("type") == "result":
            answer = ev.get("text") or ""
    if not answer:
        answer = proc.stdout[-4000:]
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


def run_odysseus(question: str) -> tuple[str, list[str]]:
    """One fresh Odysseus chat per question over /api/chat_stream (SSE).

    Agent mode by default; ODYSSEUS_MODE=chat sends what the web UI sends with the agent
    toggle off, so the eval also covers Odysseus switching the turn to agent mode itself.

    Tool-approval prompts ("Allow this task to continue?") are answered the way a user
    clicking "Allow for this task" would (ODYSSEUS_APPROVAL=approve_task|approve|deny).
    """
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
            async with httpx.AsyncClient(timeout=600) as http:
                for _ in range(8):
                    r = (await http.post(llm, json={
                        "model": os.environ.get("BAKEOFF_MODEL", "qwen3.5-35b"),
                        "messages": msgs, "tools": tools, "temperature": 0.2,
                        "chat_template_kwargs": {"enable_thinking": False}})).json()
                    m = r["choices"][0]["message"]
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
            yield q, answer, used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", choices=["raw", "hermes", "odysseus"], required=True)
    ap.add_argument("--profile", default="convo")
    ap.add_argument("--toolsets", default="convo")
    ap.add_argument("--only", type=int, nargs="*", help="question numbers (1-based)")
    args = ap.parse_args()

    data_dir = Path(os.environ["CONVO_DATA_DIR"])
    ids = conv_ids(data_dir / "convo.db")
    todo = [(i, item[0]) for i, item in enumerate(QUESTIONS, 1) if not args.only or i in args.only]
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{args.frontend}.jsonl"
    if not args.only:  # a full run replaces the previous one; --only adds to it
        out.write_text("")

    def record(i, q, answer, tools, elapsed):
        _, expected, kws = QUESTIONS[i - 1]
        s = score(answer, tools, expected, kws, ids)
        row = {"n": i, "q": q, "frontend": args.frontend, "elapsed": round(elapsed, 1),
               "tools": tools, **s, "answer": answer}
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        mark = "PASS" if s["pass"] else "FAIL"
        print(f"{mark} #{i:2d} {elapsed:5.1f}s tools={tools} {s} :: {q}", flush=True)
        return s["pass"]

    passed = 0
    if args.frontend == "hermes":
        for i, q in todo:
            t = time.time()
            answer, tools = run_hermes(q, args.profile, args.toolsets)
            passed += record(i, q, answer, tools, time.time() - t)
    elif args.frontend == "odysseus":
        for i, q in todo:
            t = time.time()
            try:
                answer, tools = run_odysseus(q)
            except httpx.HTTPError as e:
                answer, tools = f"ERROR: {type(e).__name__}: {e}", []
            passed += record(i, q, answer, tools, time.time() - t)
    else:
        # PYTHONPATH too, so a run from another checkout serves that checkout's code
        env = {k: v for k, v in os.environ.items()
               if k.startswith("CONVO_") or k == "PYTHONPATH"}
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")

        async def go():
            n = 0
            t = time.time()
            gen = _raw_session([q for _, q in todo], env)
            idx = iter(todo)
            async for q, answer, used in gen:
                i, _ = next(idx)
                n += record(i, q, answer, used, time.time() - t)
                t = time.time()
            return n

        passed = asyncio.run(go())
    print(f"\n{args.frontend}: {passed}/{len(todo)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
