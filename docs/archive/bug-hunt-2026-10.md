# Bug hunt, October 2026

A review of Athena (`server/`, `firmware/recorder/`, the PC and Android relays,
`zepp-app/`) and of the fork code in the hub ([SDarioosh/Barvis](https://github.com/SDarioosh/Barvis)).
Every bug listed below was reproduced in the cloud sandbox with a test that failed
before the fix and passes after it. Candidates that did not reproduce are listed at the end.

## Fixed here (odysseus-sound)

| # | Area | Bug | Effect | Test |
|---|---|---|---|---|
| 1 | `server/convo_server/pipeline.py` | A `pcm16` upload with a `sample_rate` other than 16 kHz was stored raw. Processing decoded it as 16 kHz. | 8 kHz audio was transcribed at double speed (a 300 Hz tone came out as 600 Hz), and the VAD spans no longer matched the audio. | `test_pcm16_at_other_sample_rate_is_transcribed_at_the_right_speed` |
| 2 | `server/convo_server/store.py` (`save_conversation`) | When a recording bridged two conversations, the absorbed conversation was deleted together with its action items, facts and fact links. | An action item the user had marked done came back as open after the merged conversation was re-summarized. Fact history was lost. | `test_merging_conversations_keeps_the_absorbed_ones_closed_action_items` |
| 3 | `server/convo_server/store.py` (`_delete_conversation`) | Deleting a conversation left the facts its facts had replaced marked `superseded` by a fact that no longer exists. | `list_facts` hid those facts for good, even though nothing current replaced them. | `test_deleting_a_conversation_restores_the_facts_it_had_replaced` |
| 4 | `server/convo_server/pipeline.py` (`Worker._maybe_digest`) | Yesterday's digest was written once and never redone. | Recordings that store-and-forward relays upload the next day were never in their day's digest. The digest is now rebuilt when that day's set of summarized conversations changes. | `test_worker_redoes_a_digest_when_a_late_conversation_of_that_day_arrives` |
| 5 | `server/convo_server/api.py` (`parse_ts`) | `timestamp=nan` / `inf` was accepted. | HTTP 500 (`NOT NULL constraint failed`) instead of 400. | `test_non_finite_timestamp_is_a_400_not_a_500` |
| 6 | `server/convo_server/diarize.py` (`cluster`) | Average-linkage clustering recomputed every cluster pair in a Python loop: O(n⁴). | 200 pieces took 15 s and 400 took 118 s. A long recording (over 1000 pieces) would hold the worker's process lock for hours. The new version uses the same linkage with an incremental similarity matrix and gives the same labels: 1500 pieces take about 10 s. | `test_cluster_matches_average_linkage_reference`, `test_cluster_is_fast_enough_for_a_long_recording` |
| 7 | `firmware/recorder/main/console_cmds.c` (`queue drop`) | `queue` printed chunk ids as zero-padded hex (`00000010`). `queue drop` parsed them with `strtoul(…, 0)`, which reads a leading `0` as octal. | Copying `00000010` (chunk 16) from the listing deleted chunk 8, possibly audio that was never uploaded. Ids are now printed in decimal, as the relays print them. The new `cq_parse_id` takes decimal or `0x` hex and refuses a leading `0`. | `test_parse_id` (host C tests) |
| 8 | `firmware/recorder/tools/barvis_relay/uploader.py` | `http.client.HTTPException` (`IncompleteRead`, `BadStatusLine`) is not an `OSError`, so it escaped `Relay.run`. | The PC relay crashed on a truncated or non-HTTP answer, including in continuous mode. No audio was lost, because nothing had been acked. It is now a retry-later. | `test_broken_http_answer_is_retry_later[...]` |

The Android relay's `HttpUploader` already handled the same two broken answers
correctly (checked in a plain Kotlin/JVM build, not the app's Gradle/AGP build).

## Fixed in the hub (SDarioosh/Barvis, same branch name)

| # | Area | Bug | Effect |
|---|---|---|---|
| 9 | `src/sound_module_view.py`, `src/module_action_items.py` | The parsers didn't allow the ` (voice note)` marker this module puts after a title in `search_conversations` and `list_action_items`. | Voice notes and their to-dos were missing from the Conversations page and were never pushed by the action-item sync. |
| 10 | Same files | The due date was split off at the first ` due `. | `Confirm the due date with Ana … due 2026-10-23` showed as the text "Confirm the", and the owner was lost. The split is now at the last ` due `. |
| 11 | `scripts/modules_setup.py` | Module skills required `mcp:<Server>` as typed, but `with_mcp_toolsets` offers the casefolded name. | With a mixed-case server name, the module skill dropped out of skill matching. |

## Found, not fixed (needs a decision or real hardware)

- **Late chunk, second diarization pass** (`pipeline.py`): a chunk that arrives after a
  session was processed is diarized on its own. Its "Speaker 1" can be a different
  person from the first pass's "Speaker 1" in the same conversation. Also,
  `put_speaker_candidates` replaces the session's stored voices, so the first pass's
  unnamed voices can no longer be remembered. This needs a design change (diarize
  against the session's earlier candidates).
- **`on_session_done` kind race** (`firmware/recorder/main/app_main.c`): it reads
  `ss.kind` after the session task has cleared `running`. A new session started in
  that window would report its own kind. This depends on timing and can only be tested
  on the device.
- **BYE after a failed upload** (protocol §6 step 5 vs §7.1): the relay still sends
  `BYE` when the server is unreachable. The watch counts that as a sync and stops
  advertising for up to 60 min, which undoes the relay's short backoff.
- **Relays differ on errors**: Kotlin treats `ERROR busy` as a watch error (backoff),
  while Python retries it. Python stops the drain on a refused `TIME`, while Kotlin
  continues.
- **One unreadable open chunk blocks the queue**: an I/O error while recovering it
  makes `cq_init` fail until the card is fixed.
- **Hub time zone**: `module_action_items` uses the container's local time (UTC in
  Docker), while the module uses `CONVO_TIMEZONE`.

## Checked, no bug found

Wire formats (protocol §8 vectors against the firmware, Python and Kotlin), the ack and
retry rules in both relays, the Ogg scanner (300k fuzzed inputs under ASan), the
30-second chunk timing, power-cut recovery, auth on every API route, HTML escaping in
`/view` and in the hub's sound page, `mcp_trust` routing, `offered_reads`,
`web_turn_scope`, `fork_deploy.sh` config parsing.
