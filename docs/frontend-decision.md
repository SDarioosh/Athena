# convo-ai knowledge frontend: Hermes Agent vs Odysseus vs Hybrid (own SQLite + MCP)

*Decision document. Researched 2026-10-18 against the copies installed on this machine:
Hermes Agent v0.22.2 (git `d923a5eb`, 2026-10-15) at `/home/steam/.hermes/hermes-agent`, and
Odysseus (git `0590edd4`, 2026-10-16, `HARNESS_VERSION` 0.20.19) at `/home/steam/models/odysseus`.
No `.env`, `auth.json` or other credential files were read.*

---

## Decision (owner, 2026-10-18)

- **Knowledge layer: hybrid.** Our SQLite store is the source of truth, exposed by our MCP
  server (`server/convo_server/mcp_server.py`).
- **Frontend: Odysseus, as our own fork** (repo `SDarioosh/Barvis`; the fork was called
  Barvis until 2026-10-19 and is now simply Odysseus). The fork is modified to work with our
  system and kept in sync with upstream (see "Odysseus fork plan" below).
- The owner chose this knowing the side-by-side numbers below. The fork's changes target
  the specific failures the test found.

## Side-by-side results (measured)

Setup: 8 fictional conversations seeded with `server/scripts/seed_demo.py` and summarized
by the real pipeline. 20 questions (`server/eval/bakeoff.py`), each in a fresh session.
Same model (qwen3.5-35b on llama-server) and the same MCP server everywhere. Odysseus ran
as an isolated native copy, not the owner's install.

A question **passes** when the frontend called a convo tool, cited a link to the right
conversation, and the answer contains the expected fact. "Content" counts the fact alone.

| Frontend | Pass | Content | Used convo tools | Cited link | Avg s |
|---|---|---|---|---|---|
| Model + MCP directly (no frontend) | 18 | 18 | 20 | 19 | 7 |
| Hermes, default config | 1 | 14 | 15 | 1 | 22 |
| Hermes, `tool_search` off + MCP instructions in `SOUL.md` | 5 | 19 | 20 | 5 | 34 |
| **Hermes, as above + citation hint in tool results** | **15** | **20** | 20 | 15 | 16 |
| Odysseus, "Regular tools" mode + instructions in a Custom preset | 1 | 9 | 13 | 2 | 46 |
| **Odysseus, as above + citation hint in tool results** | **3** | 10 | 13 | 4 | 44 |
| **Our Odysseus fork** (trusted read-only MCP, web-routing fix, built-ins trimmed), approvals *denied* | **9** | 18 | 20 | 9 | 15 |

What the test found:

- **Hermes.** By default it defers MCP tools behind `tool_search`, and it never forwards an
  MCP server's `instructions`. With `tools.tool_search.enabled: off` and the instructions in
  the profile's `SOUL.md`, it used our tools on every question. It still often cited
  conversations by title instead of by link. The citation hint now at the top of every
  non-empty tool result fixed most of that. (This corrects the TL;DR below: Hermes does
  *not* offer MCP tools directly with default settings.)
- **Odysseus.** Calls to third-party MCP tools are approved one exact operation at a time.
  After one approved call in a message, any further call to our tools is hard-blocked ("outside
  server request authority"), so the model can't follow up with a second search or open the
  conversation to get its link. Other failures:
  - Questions containing "latest" or "recommend" are classed as web requests and get an
    instant "Web access is disabled" with no tool call.
  - The model often picks Odysseus's built-in calendar, contacts or tasks tools over ours.
  - In "Auto" tool mode a non-Odysseus model goes through compact routing, which offers no
    MCP tools at all, so the test used "Regular tools".
  - No documented setting turns the per-call gate off.
- **Content errors the score doesn't catch.** The model sometimes gets the year wrong
  ("January 12th, 2026" for a move announced in October 2026). Hermes once searched the
  wrong year entirely.

## Odysseus fork plan

The goal is the smallest possible patch set, so upstream merges stay routine.

1. **Trusted read-only MCP servers.** Add a per-server option that admits tools declaring
   `readOnlyHint: true` without per-call approval or the request-authority block. Write
   tools (`update_action_item`, `name_speaker`) keep the normal approval. This is general,
   so offer it upstream; if accepted it leaves the fork.
2. **Web-intent routing.** Stop words like "latest" from short-circuiting to "web access
   disabled" when MCP tools could answer.
3. **Tool choice.** Disable the built-ins we don't use and use "Regular tools" mode. This is
   configuration, not code.
4. **Fork features**, kept in new files where possible:
   - a conversations / transcript view;
   - action items flowing into Odysseus tasks and CalDAV;
   - ntfy notifications.
5. **Branding** through theme and config, not by renaming identifiers across the codebase.

**Keeping up with upstream:**

- Odysseus is about 478k lines of Python, had 340 commits in the 30 days to 2026-10-18,
  and has no releases.
- Add an `upstream` remote and merge weekly. CI runs Odysseus's tests plus
  `eval/bakeoff.py --frontend odysseus`.
- Our code goes in new modules, with only small hooks in core files.
- Expect occasional conflicts in the agent runtime, which upstream is refactoring.

**License.** Odysseus is AGPL-3.0, which is fine for private use. If others use it over a
network they must be offered the source; a public fork does that. The convo server is a
separate program reached over MCP, so the AGPL doesn't extend to it.

**Done when** the fork passes the same 20 questions at least as well as tuned Hermes (15/20).

**Progress (2026-10-18, fork `SDarioosh/Barvis`, PR SDarioosh/Barvis#1):**
steps 1–3 are done, plus one extra fix: Odysseus counted every MCP call as an unverified
external change and appended "External operation … not independently verified" to answers.
Result: 9/20, up from 3/20. Every question now uses the convo tools, 18/20 answers contain the
right fact, and the average time dropped from 44 s to 15 s (15 tools sent instead of 101).
Approval prompts were denied in this run, so nothing passed by clicking "allow".
What's left is citation: 9 of the 11 failures have the right answer but no transcript link,
and 2 ended as a bare "Done." (the model returned empty text after its tool calls).

---

## TL;DR recommendation (original, before the test and the owner's decision)

Go with the **hybrid**. Our own SQLite + embeddings store stays the source of truth, and an MCP
server we write exposes it (`search_conversations`, `get_conversation`, `get_transcript_excerpt`,
`list_action_items`, `list_facts`, `daily_digest`). **Hermes Agent** is the first frontend, run in
a dedicated profile that is locked to the local llama-server. Neither product's built-in memory can
answer "what did X say about Y last month" with timestamps and transcript links. Hermes keeps
~3.5 KB of curated notes plus full-text search over its *own* chat history. Odysseus keeps a single
`memory.json` of short facts (≤5 injected per turn) and a generic document RAG. So grounded
retrieval has to come from our store either way. Hermes is the better MCP host for that store:
stdio/HTTP/SSE transports, per-server tool and connect timeouts, and MCP tools always offered to
the model. It also matches the access path the user already uses (`hermes serve` → Mac/phone).
Odysseus is better at acting on action items (built-in tasks/notes/calendar with CalDAV, scoped REST
tokens) and has a ready-made mobile PWA. It stays a credible second frontend on the same MCP server.
In the current deployment it also leaks: its ChromaDB answers unauthenticated on `0.0.0.0:8100`.

---

## Context & what we need

- **Pipeline (ours, frontend-independent):** Omi / T-Rex 3 Pro → local Ubuntu server → Silero VAD →
  whisper.cpp → diarization → conversation split (>3 min gaps) → llama-server
  (Qwen3.5-35B-A3B Q4_K_M, router mode, `http://127.0.0.1:8082/v1`, RTX 3060 12 GB, 46 GB RAM) →
  per-conversation JSON (title, summary, key points, action items, people, topics, durable facts) +
  daily digest.
- **Source of truth:** our SQLite (`conversations`, `segments` with speaker + start/end,
  `summaries`, `action_items`, `facts`, `people`, embeddings from a local embedding model). IDs are
  stable, so every answer can link to `http://<server>/conversations/<id>#t=<seconds>`.
- **Hard constraint:** no audio, transcripts or summaries leave the LAN. Cloud APIs are allowed
  only for dev tooling.
- **What the frontend must do:** chat from Mac and phone. Answer time-, person- and topic-scoped
  questions with citations. Surface the digest and action items. Notify the user. Ideally create
  tasks or calendar entries. Do all of this with a local model.

---

## 1. How memory works

**Hermes.**
- **Built-in memory** is two bounded, §-delimited Markdown files in `~/.hermes/memories/`:
  `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars). This is verified at
  `hermes_cli/config_defaults.py:1319-1320`, `tools/memory_tool_store.py:186` and
  `website/docs/user-guide/features/memory.md:15-20`.
- They are injected as a *frozen snapshot* at session start (memory.md:57). They do not
  auto-compact: a write that would overflow fails, and the model has to consolidate (memory.md:26-34).
  Writes go through a prompt-injection scan (`tools/memory_tool_store.py:27-31`).
- **Session search:** every Hermes chat is stored in `~/.hermes/state.db` (SQLite + FTS5,
  `hermes_state_fts.py:46`). The `session_search` tool returns raw messages, with unlimited capacity
  and no LLM in the loop (memory.md:237-258). It only indexes Hermes' *own* conversations, not
  external data.
- **Pluggable memory providers:** an ABC with prefetch, sync_turn, tool schemas and session-end
  hooks (`agent/memory_provider.py:84-180`). One provider is active at a time
  (`plugins/memory/__init__.py:1-8`).
  - Bundled: RetainDB (cloud) and ByteRover (`plugins/memory/`).
  - Catalog (`plugin-catalog/*.yaml`, 25 memory-category entries): Honcho, Mem0, Hindsight,
    Supermemory, OpenViking, Holographic, agent_memory_es and others. Holographic is a local SQLite
    + FTS5 fact store (memory-providers.md:552-570, comparison table at :795-807).
- **Months of conversation data?** Not in built-in memory: about 3.5 KB total, by design. A
  self-hosted provider (Mem0 OSS, Hindsight local, Holographic) could hold more. All of them are
  generic "facts about the user" stores. None models speakers, segment timestamps or transcript
  links.

**Odysseus.**
- **Storage:**
  - Memory is one JSON array file, `data/memory.json`. Records hold
    `{id, text, timestamp, source, category, uses, owner, pinned?, session_id?}`
    (`src/memory.py:47-49`, `:280-295`).
  - Every save rewrites the whole file (`src/memory.py:261-278`).
  - A parallel ChromaDB collection `odysseus_memories_<lane>` holds pre-computed embeddings
    (`src/memory_vector.py:6,28`). Default embedder: FastEmbed `all-MiniLM-L6-v2`
    (`src/embeddings.py:39`; `docker-compose.yml` `FASTEMBED_MODEL`), or an OpenAI-compatible
    `EMBEDDING_URL` lane (`src/embedding_lanes.py:97`).
- **Retrieval** is real hybrid scoring (`src/chat_processor.py:164-260`):
  - 0.55 × vector cosine + 0.40 × BM25 + 0.05 × recency, gated by minimum relevance.
  - Category boosts for identity, contact and preference queries.
  - Jaccard fallback when Chroma is down (`src/memory.py:361-452`).
- **Injection cap:** pinned memories plus retrieved ones, **max 5 per turn**
  (`MEMORY_CONTEXT_LIMIT = 5`, `src/chat_processor.py:92`, `:329-360`).
- **Dedup and consolidation:**
  - Exact-text dedup on add (`src/memory.py:317-323`).
  - Vector near-dup at 0.92 (`memory_vector.py:165`), and 0.72 during auto-extraction
    (`services/memory/memory_extractor.py:467`).
  - Auto-extraction keeps at most **2 facts per conversation, under 15 words each**
    (`memory_extractor.py:70-90`).
  - Every 5 new memories (`AUDIT_INTERVAL = 5`, :137), an LLM "audit" sends **all** memories to the
    model in one call to merge or delete (`memory_extractor.py:634-698`).
  - Decay: none found beyond the 5% recency term and a `uses` counter.
- **Document RAG** (`src/rag_vector.py`, collection `odysseus_rag`): 1,000-char chunks with
  200-char overlap (:622). Top-5 chunks are injected (`chat_processor.py:384`). The only metadata
  filter is `owner` (:348-370).
- **Months of conversation data?** The fact memory degrades on two counts. The whole-file rewrite
  and the all-at-once LLM audit stop scaling past a few thousand entries (the audit stops fitting a
  local model's context), and the 5-memory cap limits recall. Document RAG would hold the volume
  but has no date or person filtering. *Not load-tested; this is inferred from the code.*

**Hybrid.** Storage, schema, limits and retention are ours. SQLite + FTS5 + vector search comfortably
holds years of segments on this hardware. Time, person and topic filters are plain SQL. Dedup and
decay of `facts` happen in the pipeline, where we control them. **The hybrid wins clearly on this
criterion.**

## 2. Ingestion (programmatic push)

**Hermes.**
- **Built-in memory has no documented write API.** Writes happen through the model's `memory` tool.
  Hand-editing the files is possible, but a drift guard refuses files that don't round-trip
  (`tools/memory_tool_store.py:49-60`), and the docs warn against two writers on one home
  (memory.md:22-24).
- **Indirect paths:**
  - Webhook routes that trigger an agent run (`website/docs/user-guide/messaging/webhooks.md`).
    The write is then still model-mediated.
  - The OpenAI-compatible API server (`api-server.md`). Port 8642 is running here and returns 401
    without a key.
  - A memory-provider plugin with its own store.
  - In-process plugin tools (`ctx.register_tool`, `hermes_cli/plugins.py:456`).
- **Verdict:** usable for a *curated* push, not for bulk ingestion.

**Odysseus.**
- **Real REST ingestion:**
  - `POST /api/memory/add` (session/cookie auth; `routes/memory/memory_routes.py:56,100-147`).
  - `POST /api/codex/memory` and `POST /api/codex/documents`, using bearer API tokens with scopes
    `memory:write` / `documents:write` (`routes/codex_routes.py:402-430,513-523`;
    scopes at `routes/api_token_routes.py:14-37`).
- **File-based path:** personal-document directories indexed into RAG (`src/personal_docs.py:296-307`).
- **Limitation:** the memory payload is only `text/category/source/session_id`. There is no
  free-form metadata, so the conversation ID, date and URL would have to live inside the text.
- **Verdict:** Odysseus wins over Hermes on ingestion.

**Hybrid.** The pipeline writes straight into our SQLite, so no frontend ingestion is needed. The
frontend reads through MCP. Only an optional small curated subset is pushed into frontend memory
(see "Hybrid design notes").

## 3. Retrieval quality ("what did X say about Y last month" + source links)

**Hermes.**
- Not possible natively. Built-in memory has no transcripts. `session_search` only sees Hermes chats.
- Memory providers do semantic recall over facts, without speaker or time structure.
- Getting grounded answers with `#t=` links requires our own tool (MCP or plugin).

**Odysseus.**
- Not possible natively either:
  - Fact memory injects ≤5 short facts, with no source timestamps.
  - RAG injects top-5 text chunks, and its only metadata filter is `owner`.
  - A "last month" or "person = X" constraint can't be pushed down into retrieval.
- **Partial credit:** if conversations were pushed as documents with a header like
  "2026-09-14, Maria, URL …", semantic hits would carry the link in the text. Precision on
  time/person queries would still be poor.

**Hybrid.**
- `search_conversations(query, person, since, until)` does the filtering in SQL plus vector/FTS
  ranking, then returns excerpts with `conversation_id`, speaker, start/end and a ready-made
  transcript URL. The model only has to quote and cite.
- **Probe on this machine:** raw llama-server, Qwen3.5-35B, 3 tool schemas, 4 prompts
  (e.g. "What did Maria say about the budget last month?"). All 4 returned correct native tool
  calls with resolved dates, e.g.
  `{"query":"budget","person":"Maria","since":"2026-09-01","until":"2026-09-30"}`, in 1.3-5.0 s.
  This is a small sample *without* a frontend's large system prompt and tool list.
  **End-to-end reliability through Hermes or Odysseus is not verified.**

## 4. Local model support (llama-server) & tool-calling reliability

**Hermes.**
- **llama-server support:**
  - First-class: `providers.llamacpp.base_url`, or `local_runtime.detect_ports`
    (`website/docs/user-guide/local-models.md:135-175`).
  - This install is already configured that way: `~/.hermes/config.yaml` has
    `model.provider: llamacpp`, `default: qwen3.5-35b`, `base_url: http://127.0.0.1:8082/v1`,
    `context_length: 131072`.
- **Tool calling:** native OpenAI tool calls, with auto-repair of malformed calls
  (`agent/turn_tool_validation.py`, `agent/message_sanitization.py`,
  `guides/local-ollama-setup.md:308-316`). llama.cpp needs `--jinja`.
- **Docs' own caveats:**
  - Small local models (<30B) often *claim* memory saves without calling the tool (memory.md:78).
  - Hermes is agentic, and its default toolset is large: it keeps ~20+ core tools eager
    (`tool-search.md`). That costs context and selection accuracy on a 35B-A3B model.
  - **Mitigation:** a dedicated profile with most toolsets disabled.
- **Not verified:** a full Hermes turn with our MCP tools on Qwen3.5-35B.

**Odysseus.**
- **llama-server support:** OpenAI-compatible endpoints are added in the UI, or via
  `LLM_HOST(S)` / `host.docker.internal` (docker-compose.yml). There is a "Cookbook" for serving
  models.
- **Tool calling with local models** (Odysseus is notably defensive here):
  - Local Ollama endpoints default to *text-based fenced tool calls* (`website/setup.md:335-345`).
  - A large parser handles Qwen3 native-text, XML-invoke, DSML and raw-JSON formats
    (`src/tool_parsing.py:1083-1194`, `:288-297`, `:845-1009`).
  - Qwen-specific env switches exist (`ODYSSEUS_QWEN_NATIVE_COMPACT_BUILTINS`, docker-compose.yml).
- **Tool exposure:** tools are chosen per turn by **semantic tool retrieval**, top-K≈8 plus an
  always-on set (`src/tool_index.py:33-60, 601`).
- **Not verified:** a full Odysseus turn on our model.

**Hybrid.** Inherits whichever frontend is used. Narrow, well-described tools that take few
arguments, as probed above, are the most reliable shape for Qwen.

**Verdict:** roughly a tie. Odysseus has more local-model hardening. Hermes passes our MCP tools
through deterministically (see criterion 8), which matters more for the hybrid.

## 5. Access from devices (Mac, phone) & multi-device sync

**Hermes.**
- **Mac:** `hermes serve` is the headless JSON-RPC/WebSocket backend that the desktop app (Electron)
  connects to as a "Remote gateway" (`hermes_cli/subcommands/dashboard.py:62-73`;
  `website/docs/user-guide/desktop.md:426-520`). Here it listens on `0.0.0.0:9119`, and
  `/api/status` reports `auth_required: true, auth_providers: ["basic"]`.
- **Sync:** sessions and memory live server-side in `state.db`, so every client sees the same history.
- **Phone:**
  - The web dashboard (mobile-browser use is mentioned in `web-dashboard.md:157`), and/or
    messaging gateways (`website/docs/user-guide/messaging/`: Signal, Matrix, SimpleX, ntfy, email,
    Telegram, WhatsApp, …).
  - **Privacy:** Telegram, WhatsApp, Slack and Discord relay message content through third-party
    clouds. Only self-hosted Matrix, self-hosted ntfy, and SimpleX/Signal (E2E-encrypted) fit the
    constraint.
  - *I could not verify which phone client the user currently uses with `hermes serve`.*

**Odysseus.**
- A responsive web app that installs as a **PWA** (`static/manifest.json`, `static/sw.js`).
- Plus a "companion bridge" for LAN clients: discovery, ping/info/models, one-time pairing tokens
  minted by an admin (`companion/README.md`, `companion/routes.py`).
  *A native companion phone app was not found in the repo.*
- HTTPS for phones via mkcert/Tailscale (`website/setup.md:437-457`).
- Server-side state, so multi-device works.

**Hybrid.** Frontend-dependent. The MCP server itself is never exposed to devices. Transcript
URLs need our own small authenticated web view for `/conversations/<id>#t=`.

**Verdict:** Odysseus is somewhat better on the phone (PWA, one UI for everything). Hermes is better
on the Mac (native app) and is what the user already runs.

## 6. Privacy, auth, data isolation

**Hermes.**
- **Auth:**
  - `hermes serve` engages auth automatically on non-loopback binds: basic auth (scrypt hash
    supported) or OAuth/OIDC (desktop.md:462-500).
  - The docs say to keep basic auth on a trusted LAN or VPN only.
  - The API server needs `API_SERVER_KEY`.
- **Telemetry:** `shared_metrics` is disabled in this config (`~/.hermes/config.yaml:24-28`).
- **Main leak risk is configuration, not code:**
  - Hermes sends whatever it retrieves to the *configured provider*.
  - This same install is used for dev work with cloud models: this research session runs on a cloud
    provider.
  - Auxiliary tasks default to "inherit the main model" (`configuration.md:1386`), but an auto-chain
    can fall back to OpenRouter when keys are present (`hermes_cli/config_defaults.py:743-750`).
  - **Requirement:** a dedicated profile (`~/.hermes/profiles/<name>/`, isolated memory, sessions
    and `.env`) with no cloud credentials and no fallback providers.
- Memory providers get a secret scrub (memory-providers.md:43). Isolation is per profile
  (memory-providers.md:809-816).

**Odysseus.**
- **App-level controls are good:**
  - bcrypt passwords, TOTP 2FA (`core/auth.py:3,16,74-78,493`).
  - Multi-user owner scoping and per-user privileges, with admin-gated MCP/tokens/webhooks
    (`website/setup.md:505-520`).
  - Scoped API tokens. `AUTH_ENABLED=true` by default.
- **The deployment defaults are the problem.** `docker-compose.yml` publishes ChromaDB on
  `${CHROMADB_BIND:-0.0.0.0}:8100` and ntfy on `${NTFY_BIND:-0.0.0.0}:8091`. `setup.md:457` claims
  ntfy binds to loopback by default, which contradicts the compose file.
- **Verified on this host:**
  - `0.0.0.0:8100` answers unauthenticated: `/api/v2/heartbeat` and the collection list
    (`odysseus_memories_fastembed`, `odysseus_rag_fastembed`, `odysseus_tool_index_fastembed`)
    without credentials. Anyone on the LAN can read memory and RAG contents.
  - ntfy is on `0.0.0.0:8091`.
  - The app on `0.0.0.0:7011` correctly returns 401.
- **Breadth:** Odysseus ships shell/python tools, email, a web-research stack and SearXNG. That is
  a large attack surface for a box holding private transcripts. Cloud keys are optional; leaving
  them empty keeps it local.
- **Also on this host, for both options:** llama-server is on `0.0.0.0:8082` without auth.

**Hybrid.**
- The MCP server binds to loopback (or stdio). It is read-only by default, and write tools
  (mark action item done) are explicit.
- Our SQLite never leaves the box. The frontend only sees excerpts it asks for.
- Isolation becomes "what does the frontend's model provider see". It must be local.

## 7. Project maturity

| | Hermes Agent | Odysseus |
|---|---|---|
| Created | 2025-07-22 | 2026-05-31 (~4.5 months old) |
| Stars / forks | 254,701 / 55,367 | 93,827 / 1,652 |
| Contributors (API, anon-incl.) | ~4,595 | ~344 |
| Releases | Tagged; v0.22.0-v0.22.2 on 2026-10-14/15 | **No GitHub releases or tags**; Docker tags `X.Y.Z-<sha>`, default branch `dev` |
| Commits, last 30 days (local clone) | 16,011 (54,454 total) | 346 (2,556 total) |
| Open issues / open PRs | 16,007 / 33,782 | 1,126 / 343 |
| Closed issues | 19,672 | 1,214 |
| Last push | 2026-10-18 | 2026-10-16 |
| License | MIT | AGPL-3.0 |
| Docs | Large Docusaurus site (`website/docs`, llms.txt) | README + `website/setup.md` + internal audit notes in `docs/` |

Source: `https://api.github.com/repos/{NousResearch/hermes-agent,odysseus-dev/odysseus}` and
search API, queried 2026-10-18.

**Reading.**
- Hermes is far bigger and better documented, but its churn is extreme: about 530 commits a day,
  with tens of thousands of open PRs. Expect behaviour changes between updates, so pin versions.
- Odysseus is young and moving fast without formal releases. Its own `docs/` holds code-review and
  "fix workstream" notes, which is honest but signals instability.
- Neither one is "stable" in the conservative sense.

## 8. Extensibility: acting on action items, notifications, MCP

**Hermes.**
- **MCP client:** stdio, Streamable HTTP and SSE; OAuth 2.1; mTLS; include/exclude tool filters.
  - Per-server `timeout` (tool call, default 300 s) and `connect_timeout` (default 60 s)
    (`website/docs/reference/mcp-config-reference.md:44-70`; `tools/mcp_tool_common.py:41`).
  - Lazy start; `/reload-mcp`; dynamic `tools/list_changed` handling
    (`user-guide/features/mcp.md:746-790`).
  - Tools register as `mcp_<server>_<tool>` and enter the normal tool list (mcp.md:568-585).
    Tool Search can defer them; for a local model, configure them eager.
- **Cron:**
  - Agent or **no-agent script mode**, delivering to chat, files or platforms (`features/cron.md:18-20`).
  - This is where the daily digest push lives.
- **Other:**
  - ntfy gateway (`messaging/ntfy.md`), webhooks, kanban, hooks, plugins, skills.
  - Calendar or tasks need an MCP server (e.g. CalDAV) or a skill. Apple Reminders/Notes skills
    are Mac-only. *No built-in CalDAV was found.*

**Odysseus.**
- **Built-in action surfaces (Odysseus's clearest win):**
  - Notes/todos with reminders, scheduled agent tasks, calendar with **CalDAV sync and write-back**
    (`src/caldav_sync.py`, `src/caldav_writeback.py`, `src/task_scheduler.py`; README "Features").
  - REST endpoints for todos and calendar (`routes/codex_routes.py:245-260, 431-450`).
  - ntfy reminders, and an inbound webhook `/v1/chat` (`routes/webhook/webhook_routes.py:237`).
- **MCP client:** stdio, SSE and Streamable HTTP, with OAuth (`src/mcp_manager.py:176-200`).
  Admin-managed.
- **MCP gaps:**
  - **No per-server tool timeout**: only the browser MCP has one (90 s; `mcp_manager.py:23-27, 586-600`).
    Connect timeout is 20 s (:509-530).
  - MCP tools are **chosen by semantic retrieval per turn** (`src/tool_index.py:282-336`), so our
    tools may simply not be offered on some turns.
  - In Docker, a stdio MCP server runs *inside the container*. Ours would have to be HTTP via
    `host.docker.internal`.
  - The built-in `mcp_servers/memory_server.py` is internal stdio, not an MCP *server* for outside
    clients.

**Hybrid.**
- The MCP server can expose `update_action_item(id, status)` and a `daily_digest(date)`.
- Digest and notification push can run straight from the pipeline to self-hosted ntfy, which makes
  it frontend-independent.

## 9. Effort to integrate & maintain

- **Hermes alone:** low to start (already installed and configured), but it cannot meet the core
  retrieval requirement. Ingestion would mean fighting the 3.5 KB memory design. **Dead end alone.**
- **Odysseus alone:**
  - Medium to start. REST ingestion works, but we would reshape summaries into facts and documents
    that lose structure.
  - Medium to high to maintain: AGPL fork pressure, no releases, a broad surface to secure (fix
    Chroma/ntfy binds), and a memory model that degrades at our volume.
- **Hybrid:**
  - We build the MCP server: roughly 1-2 weeks on top of the SQLite/embedding work the pipeline
    needs anyway.
  - Frontend wiring is then about a day for Hermes (profile + `mcp_servers` entry + toolset trim +
    a short skill or memory note). About 1-2 days for Odysseus (HTTP MCP, tool-description tuning
    for its retriever).
  - Maintenance is mostly our own code, plus upgrade testing of the pinned frontend.

---

## Comparison table

| Criterion | Hermes Agent | Odysseus | Hybrid via MCP (our SQLite) |
|---|---|---|---|
| 1. Memory model | 2.2 KB + 1.4 KB curated files; FTS5 over own chats; pluggable providers. Not built for months of external data | `memory.json` facts + Chroma (MiniLM), hybrid vector/BM25, ≤5 per turn, all-memory LLM audit; RAG chunks. Degrades at scale | Our schema, SQL + FTS5 + vectors; scales; speaker/time aware |
| 2. Ingestion | No write API for built-in memory; model-mediated or plugin | **REST + scoped tokens** (`/api/codex/memory`, `/documents`), file-dir RAG | Pipeline writes directly; nothing to ingest |
| 3. Grounded "X said Y last month" + links | No | No (no date/person filter; links only if embedded in text) | **Yes**: filters + `#t=` URLs from the tool |
| 4. Local llama-server / tool calls | First-class llamacpp provider; native tools + repair; large default toolset | OpenAI-compatible endpoints; text-mode fallback; extensive Qwen parsers; tool RAG | Same as chosen frontend; probe: 4/4 correct calls on raw Qwen3.5-35B |
| 5. Devices | Mac desktop ↔ `hermes serve`; phone via dashboard/self-hosted messaging | **PWA** on phone + desktop browser; companion pairing | Frontend-dependent; need our transcript web view |
| 6. Privacy/auth | Basic/OAuth on serve; profile isolation; **risk: cloud provider in dev profile** | bcrypt + TOTP, multi-user, scoped tokens; **Chroma open on LAN here** | Loopback/stdio MCP; DB never exposed; depends on a local-only frontend profile |
| 7. Maturity | Huge, documented, tagged releases; extreme churn | ~4.5 months old, no releases, fast-moving; AGPL | Our code + pinned frontend |
| 8. Extensibility | Best MCP client (timeouts, transports, eager tools); cron, ntfy, webhooks | **Built-in tasks/calendar (CalDAV)**, ntfy; MCP via retrieval, no per-tool timeout | Write-back tools + pipeline-side ntfy; swap frontend freely |
| 9. Effort | Low setup, cannot meet core need alone | Medium setup, medium-high upkeep | MCP server 1-2 wk; ~1 day per frontend |

---

## Recommendation

**Hybrid: our SQLite + MCP server as the knowledge layer, Hermes Agent as the primary frontend,
Odysseus kept as a possible second frontend for the tasks/calendar UI.**

1. **Only the hybrid meets the core requirement.** Neither product's memory can answer time- and
   person-scoped questions with transcript links (sections 1 and 3). Our store must exist
   regardless, so putting retrieval there removes the main risk instead of moving it.
2. **Hermes is the more dependable MCP host for local models.** It has per-server tool/connect
   timeouts, stdio + HTTP + SSE, and our tools are always in the tool list. Odysseus selects MCP
   tools by semantic retrieval per turn and has no per-server tool timeout (section 8).
3. **It fits what the user already runs.** `hermes serve` with auth is live, the Mac desktop app
   connects to it, and the local llama-server is already the configured provider (sections 4 and 5).
4. **Swappable by construction.** Odysseus speaks the same MCP transports. If its PWA or calendar
   features win the user over later, it is a configuration change, not a migration.
5. **Smaller privacy surface.** No transcript content is copied into a third-party store (Chroma,
   `memory.json`). The frontend sees only the excerpts it requests, and only from a local-only
   profile.
6. **Credit to Odysseus.** It is stronger on acting on action items (native CalDAV tasks and
   calendar), on REST ingestion and on phone UX. If the user cares more about those than about
   Hermes' MCP handling, Odysseus over the same MCP server is a reasonable alternative.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Hermes profile accidentally uses a cloud model (this install is also a dev tool on a cloud provider) | Dedicated profile (e.g. `convo`): `provider: llamacpp`, no fallback providers, aux tasks inherit main, **no cloud keys in that profile's `.env`**; periodic check of outbound connections |
| Qwen misses or mis-forms tool calls inside Hermes' large prompt | Disable unneeded toolsets in the profile; keep MCP tools eager (not Tool Search); a few narrow tools with enum/ISO-date args; a skill/memory note telling it to always search before answering; measure on a fixed question set |
| Model answers from priors without citing | Tools return pre-formatted citation lines; instruction: "every claim needs a transcript link"; reject or flag uncited answers in evaluation |
| Hermes churn breaks MCP or serve behaviour on update | Pin the version; update deliberately; smoke-test the MCP tools after each update |
| Phone access through cloud messengers leaks summaries | Use only Hermes Desktop/dashboard over LAN/Tailscale, or self-hosted ntfy/Matrix; do not enable Telegram/WhatsApp/Slack/Discord for this profile |
| LAN exposure of services on this host (Chroma 8100 open, ntfy 8091, llama-server 8082, Hermes 8642/9119 on 0.0.0.0) | Bind Chroma/ntfy/llama-server to loopback or the docker bridge; reach Hermes over Tailscale; fix the Chroma bind now if Odysseus stays installed, since it holds data today |
| Transcript links unusable from the phone | Small authenticated read-only transcript view on the server (part of our API), reachable on the LAN/tailnet |
| MCP tool latency on long searches | Server-side limits (top-N, excerpt windows); Hermes `timeout` per server; return IDs + short excerpts, fetch more on demand |
| Frontend memory drifts from the source of truth | Push only a small regenerated subset (below); never treat frontend memory as authoritative |

---

## What the integration would look like (recommended option, no code)

1. **Our MCP server (`convo-mcp`)**, beside the pipeline:
   - Reads SQLite read-only, plus an explicit write tool for action-item status.
   - Transport: stdio for Hermes (spawned locally), plus Streamable HTTP on loopback / docker
     bridge if Odysseus is ever attached.
   - Tools:
     - `search_conversations(query, person?, since?, until?, limit?)`: hybrid FTS + vector ranking,
       returning id, title, date, speaker, start/end, excerpt and URL.
     - `get_conversation(id)`: summary JSON.
     - `get_transcript_excerpt(id, start, end)`.
     - `list_action_items(status?, person?, since?)` and `update_action_item(id, status)`.
     - `list_facts(person?/topic?)`.
     - `daily_digest(date)`.
   - Results are compact text with citation lines; no raw audio.
2. **Hermes profile `convo`:**
   - Local llama-server only; no fallback; trimmed toolsets.
   - One `mcp_servers` entry with explicit `timeout` and `connect_timeout`; MCP tools eager.
   - A short skill (or a `MEMORY.md` note) saying: "for anything about past conversations, call
     `search_conversations`; cite URLs".
   - Served by `hermes serve` behind auth on LAN/Tailscale.
3. **Curated push into frontend memory: yes, but small.**
   - `USER.md`: a handful of top durable facts (the user's key people and roles), regenerated by the
     pipeline from `facts`.
   - `MEMORY.md`: the tool-usage policy.
   - Everything else is reached on demand through `list_facts`. Hermes' 1,375-char `USER.md` makes
     anything bigger pointless.
   - Write it only while no session is running, or as a skill file, given the drift guard and the
     single-writer warning. For Odysseus, the equivalent would be a few pinned memories via
     `/api/codex/memory`.
4. **Digest and notifications:** the pipeline posts the daily digest and new action items to
   self-hosted ntfy directly. Optionally a Hermes no-agent cron job relays to the user's chosen
   channel.
5. **Transcript viewer:** a minimal authenticated page in our server. The JSON already exists
   (`GET /v1/conversations/<id>/transcript`, and items carry `source_url = …/transcript#t=<s>`);
   what's missing is an HTML view a phone browser can open.
6. **Evaluation:** about 30 real questions (person/time/topic). Measure tool-call rate, filter
   correctness, citation presence and latency with Qwen3.5-35B before calling it done.

---

## Open questions for the user

1. **Phone client today:** which one do you use with `hermes serve`? The dashboard in a mobile
   browser, a messaging gateway, or something else? Any cloud messenger is out for this data.
2. **Tasks and calendar:** do action items need to land in a real calendar or task system (CalDAV,
   Apple Reminders, Google)? If yes and it matters most, Odysseus as the frontend becomes more
   attractive.
3. **Separate profile:** is a separate local-only Hermes profile acceptable, and should the
   default/dev profile be blocked from attaching `convo-mcp` at all?
4. **Odysseus status:** keep it installed? If yes, OK to re-bind ChromaDB and ntfy to loopback
   (they are LAN-reachable now)? If no, should it be removed along with `data/`?
5. **Signal / SimpleX:** acceptable phone channels (E2E-encrypted but relayed), or only
   LAN/Tailscale?
6. **Write access:** should the frontend be able to *write*, i.e. mark action items done, edit
   facts, delete conversations? Or read-only for now?
7. **Retention:** how long to keep transcripts vs only summaries and facts? This affects what
   `get_transcript_excerpt` can return.

## Not verified / limitations

- End-to-end tool-calling reliability of Qwen3.5-35B *inside* Hermes or Odysseus. Only a 4-prompt
  raw llama-server probe was run.
- Odysseus memory behaviour at thousands of entries: inferred from the code, not load-tested.
- Whether a native Odysseus companion phone app exists outside this repo. Hermes' mobile
  dashboard UX.
- How the Hermes auxiliary auto-chain behaves when cloud keys are present in the profile: read from
  config comments only.
- Contributor counts are anon-inclusive GitHub pagination counts. Commit counts come from the local
  clones as of their pull dates.
- Docker container state could not be inspected (no docker socket permission); port bindings came
  from `ss`.

## Sources

**Hermes (local, `/home/steam/.hermes/hermes-agent`)**
- `hermes_cli/config_defaults.py:738-760, 1319-1320`
- `tools/memory_tool_store.py:1-60, 175-230`; `tools/memory_tool.py:56`
- `agent/memory_provider.py:84-180`; `plugins/memory/__init__.py:1-8`; `plugins/memory/{byterover,retaindb}`
- `plugin-catalog/*.yaml` (e.g. `mem0.yaml`, `hindsight.yaml`, `holographic.yaml`, `agent_memory_es.yaml`)
- `hermes_state_fts.py:46`; `tools/mcp_tool_common.py:41`; `hermes_cli/plugins.py:456`
- `hermes_cli/subcommands/dashboard.py:62-73`
- `website/docs/user-guide/features/memory.md:15-34, 57, 78, 237-258, 546`
- `website/docs/user-guide/features/memory-providers.md:26, 43-60, 388-440, 552-570, 795-816`
- `website/docs/user-guide/features/mcp.md:311-350, 568-600, 746-790`
- `website/docs/reference/mcp-config-reference.md:15-70`
- `website/docs/user-guide/features/{api-server,cron,tool-search,web-dashboard}.md`
- `website/docs/user-guide/messaging/{ntfy,webhooks}.md`
- `website/docs/user-guide/{desktop.md:426-520, local-models.md:135-175}`
- `website/docs/guides/local-ollama-setup.md:308-316`
- `website/docs/user-guide/configuration.md:1386, 2405-2421`
- `~/.hermes/config.yaml` (non-secret keys only: `model`, `providers`, `local_runtime`, `telemetry`)
- https://hermes-agent.nousresearch.com/docs ; https://github.com/NousResearch/hermes-agent
- https://api.github.com/repos/NousResearch/hermes-agent

**Odysseus (local, `/home/steam/models/odysseus`)**
- `docker-compose.yml` (secrets redacted); `README.md`; `website/setup.md:335-345, 437-457, 505-528`
- `src/memory.py:25-49, 261-323, 361-452`; `src/memory_vector.py:6-28, 104-175`; `src/memory_provider.py:96-210`
- `src/chat_processor.py:92, 164-260, 320-390`; `services/memory/memory_extractor.py:70-90, 137, 467, 525-698`
- `services/memory/service.py`; `routes/memory/memory_routes.py:56, 100-147, 342-365`
- `routes/codex_routes.py:165, 245-260, 402-450, 513-523`; `routes/api_token_routes.py:14-37`
- `src/rag_vector.py:45, 182-210, 348-370, 622-660`; `src/rag_manager.py:35-68`; `src/personal_docs.py:296-307, 452-470`
- `src/embeddings.py:7-39`; `src/embedding_lanes.py:19-20, 97`
- `src/mcp_manager.py:23-27, 176-200, 509-530, 561-620`; `src/tool_index.py:1-60, 282-336, 601`
- `src/tool_parsing.py:288-297, 845-1009, 1083-1194`; `mcp_servers/memory_server.py`; `src/builtin_mcp.py:74-75`
- `core/auth.py:3, 16, 74-78, 493`; `companion/README.md`; `static/manifest.json`; `static/sw.js`
- `routes/webhook/webhook_routes.py:237`; `src/caldav_sync.py`; `src/caldav_writeback.py`; `src/task_scheduler.py`
- `integrations/codex/README.md`
- https://github.com/odysseus-dev/odysseus ; https://api.github.com/repos/odysseus-dev/odysseus ; https://odysseus-dev.github.io/odysseus

**Live checks on this host (2026-10-18)**
- `ss -ltnp`: 7011, 8100, 8091, 8082, 8642, 9119 on 0.0.0.0; 8080 on loopback.
- `curl` results:
  - `127.0.0.1:8100/api/v2/heartbeat` and the collections list: 200, no auth.
  - `127.0.0.1:9119/api/status`: `auth_required: true`.
  - `:8642/v1/models`: 401. `:7011/api/memory`: 401.
- Raw tool-call probe against `http://127.0.0.1:8082/v1/chat/completions` (model `qwen3.5-35b`).

---

Implementation of the knowledge-layer integration is on hold pending the user's decision.
