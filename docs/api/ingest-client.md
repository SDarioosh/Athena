# Ingest client contract

What a program that uploads audio to Athena (an *uploader*) does with each answer of
`POST /v1/ingest/chunk`. The server side is `server/athena/api.py` (`ingest_chunk`)
and `server/README.md` ("Ingestion API"); this page is the client side, written once for
every uploader. The machine-readable form is
[`ingest-client-cases.json`](ingest-client-cases.json): one row per answer,
`{"status": int, "body": object | string | null, "expect": "ok" | "retry" | "drop" |
"stop_uploads"}`, where `status` 0 means no HTTP answer at all (network error, timeout,
connection closed). Uploader tests read that file instead of keeping their own list:
the meeting bot's, the PC relay's and the Android relay's (table below). A new row is
checked by all three at once.

## The request

`POST <server>/v1/ingest/chunk`, multipart, header `Authorization: Bearer <token>`.
Fields: `audio` (the file), `device`, `session_id`, `sequence`, `timestamp`, `codec`, and
optionally `sample_rate` (`pcm16` only), `kind` (`note` for a voice note), `track` and
`track_name` (one participant of a meeting). Field rules are in `server/README.md`.
Clients that can only send JSON use `POST /v1/ingest/chunk.json` (same fields, audio as
`audio_b64`); the answers are the same. The MCP HTTP app forwards `POST /ingest/chunk`
unchanged, so an uploader paired through Odysseus gets the same answers too.

The token goes only in the `Authorization` header: never in a URL, never in a log.

## Idempotency

- A chunk is identified by (`session_id`, `sequence`), or by (`session_id`, `track`,
  `sequence`) when it has a `track`; sequences then count per track.
- Sending the same chunk again is safe: the server keeps the first copy and answers
  `200` with `"ok": true, "duplicate": true` (a resent `track_name` is ignored too).
- So a client that is not sure a chunk arrived (timeout, connection cut after sending)
  **sends the same chunk again with the same identity**. It never renumbers a chunk to
  "make it new", and it never skips a sequence it has not had an answer for.

## Answers and what the client does

| Answer | Class | The client |
|---|---|---|
| `200` with `"ok": true` | **ok** | The chunk is stored (or was already: `"duplicate": true`, which is also success). Delete or acknowledge the local copy, reset the backoff, go on with the next chunk. |
| `200` with `"ok": true, "stop": true` | **ok** | As ok, and the session is over on the server (`reason`: `silence`, `max_duration`, `client`, `idle`, or the stop reason of an ended meeting). A recorder that follows the server stops recording (the Zepp app, the meeting bot: `ended server_stop` / `max_duration`). A relay that drains a device that decides for itself (the T-Watch) ignores `stop`. |
| `200` without `"ok": true` (`"ok": false`, not JSON, not an object) | **retry** | Not the Athena server's answer (a captive portal, a wrong URL): keep the chunk, retry with backoff. |
| `3xx` | **retry** | Redirects are **never followed**: following one would send the token and the audio to another host. Keep the chunk, retry with backoff, and tell the owner "redirect not followed; check the URL". |
| `401`, `403` | **stop_uploads** | The token is rejected (`403` also: a meeting bot's token used for another session, or `kind` other than `conversation`). Stop uploading, keep every unsent chunk, show "token rejected" until the settings change. Don't retry in a loop. |
| `408`, `429` | **retry** | Timeout / too many requests (usually a proxy): keep the chunk, retry with backoff. |
| other `4xx` (`400` bad field, `404` wrong path, `413` too large, `415`, `422` undecodable audio) | **drop** | The server refuses this chunk for good; sending it again gets the same answer. Log the status and `detail`, stop trying this chunk and go on with the next one. What happens to the copy depends on the client (below). `422` uploads are kept by the server in `data/rejected/` for inspection. |
| `5xx` | **retry** | Server or proxy trouble: keep the chunk, retry with backoff. |
| no answer (status 0): connection refused or reset, DNS, TLS, timeout | **retry** | Keep the chunk, retry with backoff. The chunk may have arrived; idempotency makes the resend safe. |
| anything else (`1xx`, other `2xx`) | **retry** | Unexpected: keep the chunk, retry with backoff. (The Android relay today takes any `2xx` with `"ok": true` as ok; the server only sends `200`.) |

`ok` is only ever `"ok": true` in a `200` answer. Other fields of the answer
(`speech_seconds`, `trailing_silence_seconds`, `session_seconds`, `track`, `session_id`,
`sequence`) are informational; a client must not depend on more than `ok`, `stop`,
`reason` and `duplicate`.

### Retry and backoff

- Retries keep order: a client that uploads in sequence order keeps the failed chunk at
  the front of its queue (per track for a meeting) and does not skip past it.
- Back off exponentially between attempts, capped: the meeting bot waits 1 s doubling to
  30 s; the relays wait between connections (1, 2, 5, 10, 15 min,
  `docs/twatch-ble-protocol.md` §6 step 5). A success resets the backoff.
- A cap on unsent audio is allowed (the meeting bot drops its oldest chunk past
  `--max-seconds` and logs it); a retry by itself never discards a chunk.

### Ending a session

`POST <server>/v1/sessions/<session_id>/end` (same token) ends a session (Stop). Its
answers are read with the same classes: `200` with `"ok": true` is done (it answers
`"stop": true`), `401`/`403` stop uploads, `404` means no chunk made the session yet.

## Which clients follow it

| Client | Code | Follows the contract | Tests read the cases file |
|---|---|---|---|
| Meeting bot (C++) | `meeting-bot/core/src/core.cpp` (`upload_loop`) | yes | yes: `meeting-bot/test/test_core_upload.py` (`test_ingest_client_cases`) runs the core against `meeting-bot/test/fake_ingest_server.py --mode case` for every row |
| PC BLE relay (Python) | `firmware/recorder/tools/athena_relay/uploader.py` (`classify`, `Uploader._request`, `Uploader.upload_chunk`) | yes | yes: `firmware/recorder/tools/tests/test_ingest_client_cases.py` serves every row from a local HTTP server to `upload_chunk` (`UploadError` kinds: `RETRY` = retry, `AUTH` = stop_uploads, `SKIP` = drop) |
| Android relay (Kotlin) | `android-relay/.../net/HttpUploader.kt` (`classify`) | yes | yes: `android-relay/app/src/test/java/org/athena/relay/net/IngestClientCasesTest.kt` serves every row from a JDK HTTP server to `HttpUploader.upload` (`ServerResult`: `Transient` = retry, `AuthRejected` = stop_uploads, `Rejected` = drop); the file is an input of the Gradle test tasks |
| Zepp app (JavaScript) | `zepp-app/shared/uploader.js` (`uploadEnvelope`) | **no**, see below | no |

What **drop** means per client: the meeting bot deletes the chunk from its queue (it has
no other copy to keep it for) and logs `server refused <file>, dropped: HTTP <status>`.
The relays leave the chunk on the watch unacknowledged and skip it for this connection
(the owner deletes it on the watch with `queue drop <id>`); the next connection offers it
again.

### Known gap: the Zepp app

`zepp-app/shared/uploader.js` treats every answer other than `200` the same way: it
returns `{ok: false, error: "HTTP <status> <detail>"}` and the page marks that chunk
failed; there is no retry, no backoff and no stop on `401`/`403`. It also counts a `200`
as success without checking `"ok": true`. Its fetch is the Zepp Side Service `fetch`, so
whether a `3xx` is followed depends on the phone's Zepp app, not on our code. The Zepp
app is still the recording feasibility test app; when it becomes a real uploader it
should classify answers as above and its tests should read `ingest-client-cases.json`.
