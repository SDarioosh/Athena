# convo-ai orchestrator

A build loop where Qwen (local) implements GitHub issues, Claude takes over when
Qwen gets stuck, both models review every PR, and the PR merges only when both
approve the same commit and all checks pass.

```
issue (agent-ready)
  -> worktree + branch
  -> Qwen builds until checks pass
       stuck (ESCALATE, or 3 failed attempts) -> Claude builds
  -> PR opened
  -> review round: Qwen + Claude each post APPROVE / CHANGES
       both APPROVE + CI green -> squash merge, next issue
       otherwise -> builder fixes blockers/majors, next round
  -> after 3 rounds, or on errors/budget cap -> label needs-human, ping you
```

## Setup

1. **llama-server with Qwen.** Enable `--jinja` so tool calling works, and set an
   alias so the model name is stable:

   ```
   llama-server -m <your Qwen3.5-35B GGUF> --alias qwen3.5-35b --jinja \
     -c 65536 -ngl 99 --port 8080
   ```

2. **Hermes profile.** Create a profile named `convo-ai` (see `hermes profile --help`),
   then run `hermes -p convo-ai model` and choose the custom endpoint option
   pointing at `http://127.0.0.1:8080/v1`. Add the `delegation:` block from
   `hermes/convo-ai-profile.yaml` to that profile's config.

3. **Claude credentials.** Hermes's `anthropic` provider uses `ANTHROPIC_API_KEY` or
   your Claude Code credentials (`hermes auth add anthropic`).

4. **GitHub.** Install the `gh` CLI and run `gh auth login`. In the repo settings,
   protect `main`: require the `ci` status checks, and don't require human approvals
   (the orchestrator can't approve its own PRs; review verdicts are posted as comments).

5. **Orchestrator.**

   ```
   pip install -r orchestrator/requirements.txt
   # edit orchestrator/orchestrator.yaml: set repo_path to your local clone
   ```

## Running

Create issues with the `agent-ready` label (the first run creates the labels).
Add `hard` to send an issue straight to Claude.

```
python orchestrator/orchestrator.py --issue 1   # one specific issue
python orchestrator/orchestrator.py --once      # next issue in the queue
python orchestrator/orchestrator.py             # whole queue, then exit
python orchestrator/orchestrator.py --watch     # keep polling
```

Prompts, agent transcripts, and posted comments are saved under `orchestrator/logs/`.
When an issue is handed to you, its worktree is kept next to the repo
(`../.convo-ai-worktrees/`) for inspection.

## Safety notes

- Agents run with `--yolo` (no approval prompts) so the loop can run unattended.
  Run it on a dedicated machine or VM, or set Hermes's terminal backend to Docker
  (`hermes -p convo-ai config set terminal.backend docker`).
- `metered_tokens_per_issue` caps Claude usage per issue. Start low and raise it
  once you've seen what a typical issue costs.
- Firmware PRs are only compile-checked. Before trusting merged firmware, flash it
  and check the serial output, or add a self-hosted runner with the watch attached.

## Tuning

- **Too many rounds?** Check the review logs. If reviewers raise new nitpicks each
  round, tighten the "only unresolved issues" wording in the review prompts.
- **Qwen escalates too often or too rarely?** Adjust `build_attempts` and the
  escalation section in `prompts/build.md`.
- **Swap models** by editing the `cmd` templates in `orchestrator.yaml`; any command
  that emits Hermes-style `stream-json` works.
