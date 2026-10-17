You are building convo-ai in this repository. Read AGENTS.md
first; it describes the hardware, architecture, and checks.

## Goal
A T-Watch S3 records VAD-gated Opus audio, uploads it over Wi-Fi
to a server that transcribes (whisper.cpp), diarizes, and
summarizes conversations with llama-server.

## Plan
Work through these features in order, one at a time:
1. Firmware: PlatformIO project; log AXP2101 battery % to FATFS
   every minute.
2. Firmware: PDM mic capture with VAD, Opus encoding, timestamped
   ring buffer on FATFS.
3. Firmware: Wi-Fi upload of chunks when buffer > 60%; delete only
   after server acknowledgement.
4. Server: HTTP endpoint that receives and stores chunks.
5. Server: Silero VAD + whisper.cpp transcription.
6. Server: diarization with owner voice enrollment.
7. Server: split conversations on gaps > 3 minutes; llama-server
   produces per-conversation JSON summaries and a daily digest.
8. Server: SQLite storage with embedding search.

## Process for each feature
1. Create a branch: feature/<short-name>.
2. Implement it with tests. Run every check in AGENTS.md until
   all pass.
3. Commit, push, and open a PR with `gh pr create`.
4. Get a review: use delegate_task to have a subagent review the
   PR diff (`gh pr diff`) for correctness, edge cases, embedded
   constraints, and privacy. Ask it to end with APPROVE or
   CHANGES plus a list of blocker/major issues.
5. If CHANGES: fix the blocker and major issues, push, and
   request another review. Maximum 3 rounds.
6. If APPROVE and `gh pr checks` passes: merge with
   `gh pr merge --squash --delete-branch`, then switch back to
   main and pull.
7. Update PROGRESS.md: what's done, what's next, open questions.
   Commit it to main.

## Rules
- Never merge without an APPROVE review and passing checks.
- If you're stuck after 3 rounds or 3 failed attempts on the same
  problem, delegate the problem to a subagent for help. If that
  fails too, write the blocker in PROGRESS.md, skip to the next
  feature that doesn't depend on it, and continue.
- Never commit secrets or audio files.
- Note anything that needs testing on real hardware in the PR
  description.
- Keep going until every feature is merged or blocked.
