You are reviewing the pull request for GitHub issue #$issue_number in the
convo-ai repository (review round $round). The current directory is the PR
branch. Do not modify, create, or commit any files.

## Issue #$issue_number: $issue_title

$issue_body

## Your focus: does it do what the issue asks?

- Every requirement in the issue is implemented, and nothing unrelated was added.
- Tests exist for new server behavior and actually exercise it.
- Code follows the conventions in AGENTS.md.
- No secrets, credentials, or audio files are committed.

## Diff against main

```diff
$diff
```

## Output

Read any files you need for context. Then end your reply with exactly one
fenced JSON block:

```json
{"verdict": "APPROVE or CHANGES", "summary": "one or two sentences", "issues": [{"severity": "blocker|major|minor", "file": "path", "detail": "what is wrong and how to fix it"}]}
```

Use CHANGES only when there is at least one blocker or major issue. Minor
issues alone mean APPROVE (still list them). In rounds after the first, only
raise issues that are still unresolved; don't introduce new minor nitpicks.
