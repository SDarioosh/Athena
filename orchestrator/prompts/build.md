You are working on GitHub issue #$issue_number in the convo-ai repository.
The current directory is a git worktree on branch `$branch`.

$mode_instruction

## Issue #$issue_number: $issue_title

$issue_body

$feedback_section
$checks_section
$prior_section
## Rules

- Read AGENTS.md first and follow it.
- Do only what this issue asks. Keep the change small and focused.
- Add or update tests for server code. Firmware must build with PlatformIO.
- Run the checks listed in AGENTS.md before you finish, and fix what fails.
- Commit your work with clear messages. Do not push, open PRs, merge, or switch
  branches; the orchestrator handles that.
- Never commit secrets, API keys, Wi-Fi credentials, or recorded audio.

## When to escalate

If you are genuinely stuck (behavior you can't diagnose after real effort, a
design decision you're not confident about, or hardware behavior you can't
verify from code), stop and make the last line of your reply:

ESCALATE: <one sentence on what's blocking you>

Otherwise, end with a short summary of what you changed and how you tested it.
