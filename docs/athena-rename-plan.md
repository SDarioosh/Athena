# Athena rename: work order

Status: **planned, not started** (written 2026-10-27). On 2026-10-29 the owner also chose to
rename the hub's stored `"odysseus-sound"` values and server name `convo` with a data
migration (cleanup lane 4F; filed calendar event IDs stay), and the watch's Bluetooth name
(lane 1I); the "What stays" table below predates that. Since 2026-10-29 the steps run
inside the cleanup ([cleanup-plan.md](cleanup-plan.md), "Renaming to Athena" maps each step
to its lane). Each step below is one PR. Tick it off here in the same PR that does it.

The project was called convo-ai, then barvis-ai, barvis-sound and odysseus-sound. The
prose already says Athena. This plan renames the old names in code, settings and
services too. It leaves alone the few names that live in the owner's stored data or in
installed devices (see [What stays](#what-stays-and-why)).

## Rules for every step

- **The old name keeps working.** Read the new name first and fall back to the old
  one. This is the pattern the hub's `scripts/fork_deploy.sh` already uses for
  `BARVIS_*` → `FORK_*`. The server, the phone, the watches and the hub must keep
  working after each step on its own, in any deploy order.
- **No data migrations.** No step rewrites rows in `convo.db` or in the hub's data.
- **One concern per PR.** Do a mechanical rename as its own PR, with no behaviour
  changes mixed in, so the diff can be reviewed by `git diff -M --stat`.
- **Do the big renames (steps 3, 6, 10) when no other PR is open** in that repo. They
  touch nearly every file and would conflict with work in progress.
- **MCP contract unchanged.** Tool names, arguments, annotations, links and auth stay
  the same, so no hub preset change and no eval rerun is needed. If a step finds it
  must change one of them, stop: that is a breaking change under the
  [versioning rules](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/modules.md#versioning).
- **Checks before each push:**
  - Athena: the [Checks](../AGENTS.md#checks) for the parts touched.
  - Hub: the focused tests in its `AGENTS.md`, plus the fork tests for the files
    touched.
- **Server verification:** every step that changes deployment (7, 8, 9, 11, 12, 13)
  has a Server verification section in its PR. It targets the test instance on
  `127.0.0.1:7010`, never production.
- **Update the "don't rename" lists** (`AGENTS.md` here, `AGENTS.md` in the hub) in the
  same PR that renames a name on them.

## Order at a glance

| # | Repo | Step | Depends on | Needs a deploy |
|---|---|---|---|---|
| 0 | both | Answer the open questions | none | no |
| 1 | both | Fix stale links and the hub's deploy remote lookup | 0 | no |
| 2 | Athena | MCP server's own name `convo-ai` → `athena` | none | yes (no steps) |
| 3 | Athena | Python package `convo_server` → `athena`, CLI `athena` | none | yes (no steps) |
| 4 | Athena | PC relay package `barvis_relay` → `athena_relay` | none | no |
| 5 | Athena | Firmware macros `BARVIS_*` → `ATHENA_*` | none | no |
| 6 | Athena | Android Kotlin package `org.barvis.sound.relay` → `org.athena.relay` | none | phone update |
| 7 | Athena | Settings `CONVO_*` → `ATHENA_*`, old names still read | 3 | yes |
| 8 | Athena | Database file `convo.db` → `athena.db` | 7 | yes |
| 9 | Athena | `deploy.sh`: `ATHENA_*` settings, `athena*` services, Docker project `athena` | 3, 7 | yes, one-time steps |
| 10 | hub | Fork files `sound_module_*`, `soundModule.js`, `sound_relay_*` → `athena_*` | none | yes (no steps) |
| 11 | hub | Settings `SOUND_MODULE_SERVER` and the like → `ATHENA_*`, old names still read | 10 | yes |
| 12 | hub | Page `/modules/athena` (old URL redirects), `/api/modules/athena/*` | 10 | yes |
| 13 | hub | Relay routes `/api/codex/athena/*` and scopes `athena:*` next to the old ones | 12 | yes |
| 14 | Athena | Android relay uses the new routes, falls back to the old ones | 13 | phone update |
| 15 | Athena | Zepp test app display name "Convo Rec Test" → "Athena Rec Test" | none | watch reinstall |
| 16 | both | Final docs pass; mark this plan done | all | no |

Steps 2 to 6 and step 10 are independent of each other and can be done in any order.

## Step 0: open questions for the owner

1. Is `SDarioosh/odysseus-expanded` the hub repo `SDarioosh/Barvis` renamed, or a
   separate repo? The answer decides what the links in step 1 point to. Today the
   sandbox clone's remote is `SDarioosh/odysseus-expanded`, while the docs in both
   repos link to `SDarioosh/Barvis`.
2. Do any owner-side scripts outside these repos use the old names? For example,
   backups that copy `server/data/convo.db`, cron jobs that call `convo-server`, or
   monitoring of the `barvis-sound*` services. List them in the step 8 and 9 PRs, so
   their Server verification covers them.
3. Should the hub's module doc `docs/fork/modules/odysseus-sound.md` become
   `docs/fork/modules/athena.md`? If yes, step 1 keeps a one-line stub at the old path,
   because Athena's `AGENTS.md` and older PRs link to it.

## Step 1: stale links and the deploy remote lookup (both repos)

- **Athena: links to `SDarioosh/Barvis`** in `AGENTS.md`, `MISSION.md`, `PROGRESS.md`,
  `README.md`, `server/README.md` and `docs/*.md` (43 links). Point them at the hub's
  current URL (question 0.1). Leave links inside past QC and bug-hunt reports alone;
  they are records.
- **Athena: links to `SDarioosh/barvis-sound`** become `SDarioosh/Athena`.
- **Hub: links to `SDarioosh/Barvis`** in `AGENTS.md`, `FORK.md` and `docs/fork/*.md`.
- **Hub bug, fix in this step:** `remote()` in `scripts/fork_deploy.sh` looks for a
  remote whose URL ends in `sdarioosh/barvis`. A clone of
  `SDarioosh/odysseus-expanded` doesn't match, so the remote is only found when
  `FORK_REMOTE` is set. Match both names. Add a case to the script's test if one
  exists; otherwise test by hand with a scratch clone.
- **Athena's own lookup already matches** `SDarioosh/Athena` (`server/scripts/deploy.sh`);
  only its header comment for `BARVIS_SOUND_REMOTE` still names `*/barvis-sound or
  */barvis-ai`. Fix the comment.
- Optional (question 0.3): rename the hub's module doc and leave a stub at the old path.

Done when: `git grep -E 'SDarioosh/(Barvis|barvis-sound)'` finds only past records in
both repos (or only the redirect stub), and both deploy scripts find their remote on a
clone of the current URL.

## Step 2: MCP server's own name (Athena)

- `server/convo_server/mcp_server.py`: `MCPServer("convo-ai", …)` becomes
  `MCPServer("athena", …)`. The hub doesn't read this name; it uses the name the owner
  registered (`convo`). Check that with `git grep convo-ai` in the hub (no hits on
  2026-10-27) and in Hermes' eval profile.
- `FastAPI(title="convo-ai server")` in `api.py` and the package docstrings say Athena.
- Bump the MCP version's patch number and note in `server/README.md` that only the
  server's self-reported name changed.

## Step 3: Python package and CLI (Athena)

- `git mv server/convo_server server/athena`, and change the imports in `server/`
  (about 50 files), `server/Dockerfile`, `server/eval/`, `server/scripts/`, and the
  `server/tests` file that imports the PC relay.
- `server/pyproject.toml`:
  - `name = "athena-server"`;
  - console scripts `athena = "athena.cli:main"`, and keep
    `convo-server = "athena.cli:main"` so the deployed services and any cron jobs keep
    working until step 9;
  - update the ruff and pytest paths.
- Don't keep a `convo_server` import stub: nothing outside `server/` imports it. Check
  with `git grep -l convo_server` in both repos.
- Update `.github/workflows/ci.yml` paths.
- Done when: `ruff check .` and `pytest -q` pass, `convo-server --help` and
  `athena --help` both work after `pip install -e .`, and the Docker image builds.

## Step 4: PC relay package (Athena)

**Done** (2026-10-29): `athena_relay`, with `barvis_relay/` as a stub that aliases each
submodule (`barvis_relay.uploader is athena_relay.uploader`); the state dir rule is tested
for old dir only, new only, both and neither (`tools/tests/test_relay_names.py`).

- `git mv firmware/recorder/tools/barvis_relay firmware/recorder/tools/athena_relay`,
  then change the imports in `ble_relay.py`, `pda_relay.py`, the tests (`tools/tests/`,
  10 files), the one `server/tests` file and `ci.yml`.
- Keep `barvis_relay/__init__.py` as a stub that re-exports `athena_relay`, with a
  comment saying why, so `python -m barvis_relay…` commands in old notes still work.
  Remove it in step 16.
- The relay's state dir (`default_state_dir` in `anchors.py`, today
  `<state base>/barvis-relay`) becomes `athena-relay`, but an existing `barvis-relay`
  dir is used as is when `athena-relay` doesn't exist, so already-acknowledged chunks
  aren't uploaded twice. Never move or copy the files. Test both cases.
- Done when: `python -m pytest -q firmware/recorder/tools` passes.

## Step 5: firmware macros (Athena)

**Done** (2026-10-29): the Opus tags were string literals in `session_rec.c`, not macros; they
and the UUID bytes are now `ATHENA_TAG_*` and `ATHENA_UUID_BYTES` in `main/wire_ids.h`,
with the same bytes. `test/test_wire_ids.c` pins every UUID and tag byte against literals
copied from the firmware before the rename. `idf.py build` runs in CI.

- In `firmware/recorder/main/ble_sync.c` and the other files that use them (13 files),
  rename `BARVIS_UUID` and the `BARVIS_*` Opus tag macros to `ATHENA_*`.
- **The values stay the same:** the UUID bytes, the Opus tag bytes and
  `DEVICE_NAME "barvis-tw"`. They are the wire protocol, which
  `docs/twatch-ble-protocol.md` describes and the phone and PC relays rely on.
- Done when: the host tests pass and `idf.py build` passes for all three boards. Byte
  check: build the BLE service table before and after, and compare it (a
  `ble_uuid128_t` dump in a host test, or the `.rodata` of `ble_sync.o`).

## Step 6: Android Kotlin package (Athena)

- Move `android-relay/app/src/{main,test}/java/org/barvis/sound/relay` to
  `org/athena/relay` (57 files), change the `package` and `import` lines, and set
  `namespace = "org.athena.relay"` in `app/build.gradle.kts`.
- **Keep `applicationId = "org.barvis.sound.relay"`.** It is the app's identity: with
  it unchanged, the installed app updates in place and keeps its pairing, token and
  upload queue. Add a comment next to it saying why.
- Check `AndroidManifest.xml` for fully qualified class names (services, receivers) and
  check that `BuildConfig` and `R` imports follow the new namespace.
- Done when: `./gradlew assembleDebug test lint` passes. Real device: install over the
  current app and check that it is still paired and drains the watch (PR's Server
  verification section).

## Step 7: settings `CONVO_*` → `ATHENA_*` (Athena)

- About 98 variables. `server/convo_server/config.py` (pydantic `BaseSettings`,
  `env_prefix="CONVO_"`) reads `ATHENA_<name>` first and falls back to `CONVO_<name>`.
  Do this with a custom settings source or `AliasChoices` per field, not by copying
  `os.environ`.
- If both names are set with different values, the new one wins and the server logs
  one warning naming the variable, without its value (tokens are settings too).
- `CONVO_INGEST_TOKEN` stays required: either name satisfies the startup check.
- Also read both names in:
  - `env_value` in `server/scripts/deploy.sh`;
  - `server/compose.yml`;
  - the eval and scripts under `server/`;
  - the Zepp side service, if it reads any;
  - `ci.yml`.
- `server/.env.example` lists only the new names, with one line saying the old
  `CONVO_*` names still work.
- Docs (`server/README.md`, `AGENTS.md` and the hub's sandbox and module docs) use the
  new names.
- Tests: a setting given only by its old name, only by its new name, by both (new wins,
  one warning), and the token check with either name.
- Server verification: restart with the owner's unchanged `server/.env` (old names);
  `/health` and an MCP read work.

## Step 8: database file (Athena)

- `Settings.db_path` becomes `data_dir / "athena.db"`. On start:
  - only `convo.db` exists: rename it, and its `-wal` and `-shm` files, to `athena.db`
    while nothing has it open;
  - both exist: refuse to start, and name both paths in the error;
  - neither exists: create `athena.db`.
- The rename runs once, in the process that opens the database first (`serve`). `mcp`
  and the CLI wait for it or open whichever file exists, so they never create an empty
  `athena.db` next to `convo.db`.
- Update `server/eval/bakeoff.py`, `seed_demo.py`, the tests and the docs.
- Tests: each of the three cases, and a concurrent second opener.
- Server verification:
  1. Back up `server/data/` first.
  2. Deploy; expect `athena.db` and no `convo.db`, with the same conversation count.
  3. Owner: update any backup job that copies `convo.db` (question 0.2).

## Step 9: deploy.sh, services and Docker (Athena)

- `BARVIS_SOUND_*` settings become `ATHENA_*` (`ATHENA_REMOTE`, `ATHENA_BRANCH`,
  `ATHENA_EXTRAS`, `ATHENA_WHISPER_CMD`, `ATHENA_RUNNER`, `ATHENA_ODYSSEUS_DIR`). The
  old names are still read.
- Services `barvis-sound`, `barvis-sound-mcp` and `barvis-sound-whisper` become
  `athena`, `athena-mcp` and `athena-whisper`. They run `.venv/bin/athena` (step 3).
- One-time migration in `deploy.sh`, safe to run twice:
  1. If an old unit exists: stop and disable it, and remove its unit file.
  2. Install and start the new unit on the same host and port.
  3. Log what was migrated.
  4. With `background` runner: stop the old pid files' processes and write new ones.
- Docker: in `server/compose.yml`, the project name `odysseus-sound` becomes `athena`,
  and the service names `sound-api`/`sound-mcp` become `api`/`mcp`. Data is a bind
  mount of the data dir, so nothing is lost. `deploy.sh` runs
  `docker compose -p odysseus-sound down` once when old containers exist.
- The hub's `scripts/fork_deploy.sh` calls this script through `FORK_MODULES`; its docs
  example path `~/projects/barvis-sound/server/scripts/deploy.sh` uses the new folder
  name if the owner renamed the folder.
- Tests: `deploy.sh units` prints the new units; a dry run on a machine with old units
  lists the migration.
- Server verification:
  1. On the test instance first, if it runs its own Athena; otherwise follow the
     owner's deploy steps.
  2. Expect `systemctl status athena athena-mcp`: active.
  3. Expect the old units to be gone.
  4. Check that Odysseus still connects to the MCP URL (same port).

## Step 10: hub fork files (hub)

- Rename the fork files, not upstream files:

  | Old | New |
  |---|---|
  | `routes/sound_module_routes.py` | `routes/athena_module_routes.py` |
  | `routes/sound_relay_routes.py` | `routes/athena_relay_routes.py` |
  | `src/sound_module_view.py` | `src/athena_module_view.py` |
  | `static/sound-module.html` | `static/athena-module.html` |
  | `static/js/soundModule.js` | `static/js/athenaModule.js` (`mountSoundModule` → `mountAthenaModule`) |
  | `tests/test_sound_module_view.py`, `tests/test_sound_relay.py` | `tests/test_athena_*` |

- Update the hooks that name them: `app.py` (two router imports, the
  `AUTH_EXEMPT_EXACT` comment), `static/index.html` (sidebar link comment),
  `static/js/forkModuleWindows.js`, and `static/js/admin.js` (comment). Keep the DOM ids
  (`tool-sound-module-btn`, `rail-sound-module`) unless a test shows nothing stored
  refers to them. Shortcuts are stored by action name (`open_conversations`), not by id.
- Update `FORK.md` (the table) and the fork file list in the hub's `AGENTS.md`.
- No URL, route or behaviour changes in this step.
- Done when: the fork tests for these files pass, `py_compile` passes and
  `node --check` passes for the JS files.

## Step 11: hub settings (hub)

- `SOUND_MODULE_SERVER` (`src/call_recorder.py`, `routes/*_module_routes.py`) becomes
  `ATHENA_MODULE_SERVER`. `SOUND_MODULE_SERVER` and the older `BARVIS_SOUND_SERVER`
  are still read.
- Rename the other module settings with sound or convo in their names the same way.
- `ACTION_ITEMS_SERVER` and `ODYSSEUS_TRUSTED_READONLY_MCP` keep their names. Their
  value stays `convo` (see [What stays](#what-stays-and-why)).
- Tests: each setting by its old and its new name.

## Step 12: hub page and API routes (hub)

- `/modules/athena`, `/modules/athena/record` and `/modules/athena/pair` serve the
  pages. The `/modules/sound…` URLs answer `301` to the new ones, keeping the
  `#c=<id>` deep links (the fragment stays in the browser, so a plain redirect keeps
  it).
- `/api/modules/athena/*` is added. `/api/modules/sound/*` stays as an alias for the
  same handlers, at least until step 16, for open tabs and bookmarks. The JS calls the
  new paths.
- Sidebar link, `/open conversations`, shortcuts and the Ask-in-chat link use the new
  page.
- Tests: the old URLs redirect, the old API paths still answer, and the transcript-link
  interception in `forkModuleWindows.js` still opens the reader.

## Step 13: hub relay routes and token scopes (hub)

- `/api/codex/athena/*` serves the same handlers as `/api/codex/sound/*`. The old
  paths stay, because installed phone relays call them.
- Scopes `athena:ingest`, `athena:read` and `athena:write` are added to
  `ALLOWED_SCOPES`. Each relay route accepts the new scope or the old one, so existing
  tokens keep working.
- New pairings (`RELAY_SCOPES` in `src/phone_pairing.py`) get both the new and the old
  scopes, so a phone on an older relay version still works after pairing.
- `static/js/admin.js` lists the new scopes. Old scopes on existing tokens show as
  "(old name)".
- Tests: old token + old path, old token + new path, new token + old path, new token +
  new path; pairing gives both scope sets.

## Step 14: Android relay on the new routes (Athena)

- `net/Routes.kt` uses `/api/codex/athena/…`. On a `404` from an older hub, it falls
  back to `/api/codex/sound/…` once and remembers which works.
- README and `docs/android-app-plan.md` say the new routes and scopes.
- Done when: the Gradle checks pass, plus a real device against the test instance
  (both a new hub and a hub from before step 13).

## Step 15: Zepp test app name (Athena)

**Done** (2026-10-29), also the page title, the settings heading and the microphone hint.
The real-device install is in that PR's Server verification.

- `zepp-app/app.json`: `appName` "Convo Rec Test" becomes "Athena Rec Test", in both
  places. `appId` (1092611) and the vendor stay the same, so the watch treats it as the
  same app.
- Real device: install over the current app and record one chunk.

## Step 16: final pass (both repos)

- Remove the `barvis_relay` stub (step 4) if nothing still uses it.
- `AGENTS.md` in both repos: the "don't rename" lists keep only the names under
  [What stays](#what-stays-and-why).
- Remove the `/api/modules/sound/*` aliases (step 12) if the owner agrees. Keep the
  `/modules/sound` redirect and the `/api/codex/sound/*` routes for good: phones and
  bookmarks outlive a PR.
- `PROGRESS.md`: done. This file: status **done**, with the PR of each step.

## What stays, and why

These are identities in stored data or in installed devices. Renaming them costs a data
migration or a reinstall, and the owner never sees them in the UI or the chat. If one
is ever renamed, it gets its own plan, with a backup and a rollback.

| Name | Where | What a rename would break |
|---|---|---|
| Hub MCP server name `convo` (tools `mcp__convo__*`) | hub config, `ODYSSEUS_TRUSTED_READONLY_MCP`, `/athena` skill (`mcp:convo`), approvals, tool toggles | Filed calendar events and to-dos carry IDs with the server name (`odysseus-sound:item:convo:<id>`, `src/module_action_items.py`). After a rename, the hub no longer recognizes them: items are filed twice and status sync stops. Approvals, per-tool disables and the trusted-server setting would also all need redoing. |
| Stored `"odysseus-sound"` values | hub: belief source (`src/evidence_intake.py`), note label (`src/module_action_items.py`, `src/notes_evidence.py`), memory source (`src/today_insight.py`), calendar uid prefix | Needs a migration of the owner's hub data. Anything missed is filed or remembered twice. |
| Android `applicationId` `org.barvis.sound.relay` | installed phone app | It becomes a separate app: reinstall, pair again, and recordings waiting in the old app are lost. |
| Zepp `appId` and vendor | installed watch app | It becomes a new app on the watch. |
| BLE UUIDs, Opus tag bytes (the BLE name `barvis-tw` is renamed after all: cleanup lane 1I, relays accept both names first) | T-Watch firmware, phone and PC relays | Every watch needs reflashing and pairing again. |
| Hermes profiles `convo-ai`, `convo` | owner's Hermes setup, outside these repos | The build loop and eval profile; only the owner can rename them. |
| Hub branch `barvis` | hub repo, clones, `fork_deploy.sh` default ref | Every clone and the deploy default would need updating, for no visible gain. |
| `/api/codex/sound/*`, scopes `sound:*` | installed phone relays, existing API tokens | They stay as aliases next to the new names (steps 13, 14). |

## Related

- The slash command is already renamed: `/sound` → `/athena` in the hub
  (SDarioosh/odysseus-expanded#102). It needs no step here.
