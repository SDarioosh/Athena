#!/usr/bin/env python3
"""
convo-ai build loop.

Qwen (local, through Hermes + llama-server) implements GitHub issues.
Claude (through Hermes, Anthropic provider) takes over when Qwen is stuck.
Every PR is reviewed by both models. The orchestrator merges only when both
approve the same commit and all checks pass; otherwise it hands off to a human.

Usage:
  python orchestrator.py              # work through every agent-ready issue, then exit
  python orchestrator.py --once       # one issue, then exit
  python orchestrator.py --issue 12   # a specific issue
  python orchestrator.py --watch      # keep polling for new issues
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

HERE = Path(__file__).resolve().parent


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------- helpers

def run(cmd, cwd=None, check=True, timeout=None, shell=False):
    p = subprocess.run(cmd, cwd=cwd, shell=shell, text=True,
                       capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd}\n{p.stdout}\n{p.stderr}")
    return p


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "task"


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} chars clipped] ...\n\n{text[-half:]}"


def section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}\n" if body and body.strip() else ""


# ---------------------------------------------------------------- context

class Ctx:
    def __init__(self, cfg_path: Path):
        self.cfg = yaml.safe_load(Path(cfg_path).read_text())
        self.repo = Path(self.cfg["repo_path"]).expanduser().resolve()
        self.base = self.cfg.get("base_branch", "main")
        self.wt_root = self.repo.parent / f".{self.repo.name}-worktrees"
        self.logs = HERE / "logs"
        self.prompts = HERE / "prompts"
        self.labels = self.cfg["labels"]
        self.limits = self.cfg["limits"]
        self.roles = self.cfg["roles"]
        self.issue_tokens = 0

    def agent(self, name):
        return self.cfg["agents"][name]

    def check_budget(self, name):
        cap = self.limits.get("metered_tokens_per_issue")
        if self.agent(name).get("metered") and cap and self.issue_tokens >= cap:
            raise BudgetExceeded(f"Metered token budget for this issue used up ({self.issue_tokens:,} tokens).")

    def spend(self, name, tokens):
        if self.agent(name).get("metered"):
            self.issue_tokens += tokens


def notify(ctx: Ctx, msg: str):
    print(f"[convo-ai] {msg}", flush=True)
    cmd = ctx.cfg.get("notify_cmd")
    if cmd:
        run([part.format(message=msg) for part in cmd], check=False)


# ---------------------------------------------------------------- git / gh

def git(cwd, *args, check=True):
    return run(["git", *args], cwd=cwd, check=check)


def gh(ctx: Ctx, *args, check=True):
    return run(["gh", *args], cwd=ctx.repo, check=check)


def make_worktree(ctx: Ctx, branch: str) -> Path:
    git(ctx.repo, "fetch", "origin", ctx.base)
    path = ctx.wt_root / branch.replace("/", "_")
    if path.exists():
        git(ctx.repo, "worktree", "remove", "--force", str(path), check=False)
    ctx.wt_root.mkdir(parents=True, exist_ok=True)
    git(ctx.repo, "worktree", "add", "-B", branch, str(path), f"origin/{ctx.base}")
    return path


def remove_worktree(ctx: Ctx, wt: Path):
    git(ctx.repo, "worktree", "remove", "--force", str(wt), check=False)


def head(wt: Path) -> str:
    return git(wt, "rev-parse", "HEAD").stdout.strip()


def commit_all(wt: Path, msg: str):
    git(wt, "add", "-A")
    if git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(wt, "commit", "-m", msg)


def commits_ahead(ctx: Ctx, wt: Path) -> int:
    return int(git(wt, "rev-list", "--count", f"origin/{ctx.base}..HEAD").stdout.strip())


def push(wt: Path, branch: str):
    git(wt, "push", "-u", "origin", branch, "--force-with-lease")


def next_issues(ctx: Ctx, number: int | None):
    fields = "number,title,body,labels"
    if number:
        return [json.loads(gh(ctx, "issue", "view", str(number), "--json", fields).stdout)]
    out = gh(ctx, "issue", "list", "--state", "open", "--label", ctx.labels["ready"],
             "--search", "sort:created-asc", "--limit", "50", "--json", fields).stdout
    return json.loads(out)


def post_comment(ctx: Ctx, kind: str, number: int, body: str, log_dir: Path):
    f = log_dir / f"comment-{time.time_ns()}.md"
    f.write_text(body)
    gh(ctx, kind, "comment", str(number), "--body-file", str(f), check=False)


# ---------------------------------------------------------------- agents

@dataclass
class AgentResult:
    ok: bool
    text: str
    tokens: int


def run_agent(ctx: Ctx, name: str, template: str, vars_: dict, wt: Path,
              log_dir: Path, tag: str) -> AgentResult:
    ctx.check_budget(name)
    cfg = ctx.agent(name)
    prompt = Template((ctx.prompts / template).read_text()).safe_substitute(vars_)
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{name}"
    prompt_file = log_dir / f"{stem}.prompt.md"
    prompt_file.write_text(prompt)
    cmd = [part.format(prompt_file=str(prompt_file), workdir=str(wt)) for part in cfg["cmd"]]

    print(f"  -> {name}: {tag}", flush=True)
    try:
        p = run(cmd, cwd=wt, check=False, timeout=cfg.get("timeout_min", 60) * 60)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, rc = "TIMEOUT", 124

    # Hermes --format stream-json: one JSON event per line, final event type "result".
    text, tokens = out, 0
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            text = ev.get("text") or ""
            tokens = int((ev.get("tokens") or {}).get("total") or 0)
            rc = ev.get("exit_code", rc)

    (log_dir / f"{stem}.out.jsonl").write_text(out + (f"\n--- stderr ---\n{err}" if err else ""))
    ctx.spend(name, tokens)
    return AgentResult(rc == 0, text, tokens)


def issue_vars(issue: dict, branch: str) -> dict:
    return {
        "issue_number": str(issue["number"]),
        "issue_title": issue["title"],
        "issue_body": issue.get("body") or "(no description)",
        "branch": branch,
    }


# ---------------------------------------------------------------- checks

def run_checks(ctx: Ctx, wt: Path) -> tuple[bool, str]:
    failures = []
    for c in ctx.cfg.get("checks", []):
        req = c.get("when_exists")
        if req and not (wt / req).exists():
            continue
        try:
            p = run(c["cmd"], cwd=wt, check=False, shell=True,
                    timeout=c.get("timeout_min", 20) * 60)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, "TIMEOUT"
        if rc != 0:
            failures.append(f"### {c['name']} (exit {rc})\n```\n{clip(out, 6000)}\n```")
    return not failures, "\n\n".join(failures)


def github_checks(ctx: Ctx, pr: int) -> tuple[bool, str]:
    if not ctx.cfg.get("wait_for_github_checks"):
        return True, ""
    time.sleep(20)  # give GitHub time to register the workflow run
    p = gh(ctx, "pr", "checks", str(pr), "--watch", "--interval", "30", check=False)
    out = p.stdout + p.stderr
    if "no checks reported" in out.lower():
        return True, ""
    return p.returncode == 0, out


# ---------------------------------------------------------------- build

ESCALATE_RE = re.compile(r"^ESCALATE:\s*(.+)$", re.MULTILINE)


def build(ctx, agent, issue, branch, wt, log_dir, feedback="", prior="") -> tuple[bool, str]:
    """One agent works until local checks pass. Returns (success, detail)."""
    check_output = ""
    for attempt in range(1, ctx.limits["build_attempts"] + 1):
        if feedback:
            mode = "Address the review feedback below. Fix every blocker and major item."
        elif attempt == 1 and not prior:
            mode = "Implement the issue below."
        else:
            mode = "Continue implementing the issue below. Earlier work is already on this branch."
        vars_ = issue_vars(issue, branch) | {
            "mode_instruction": mode,
            "feedback_section": section("Review feedback to address", feedback),
            "checks_section": section("Failing checks from the last attempt", check_output),
            "prior_section": section("Notes from the previous agent, which got stuck", prior),
        }
        res = run_agent(ctx, agent, "build.md", vars_, wt, log_dir, f"build{attempt}")
        commit_all(wt, f"{agent}: #{issue['number']} attempt {attempt}")

        m = ESCALATE_RE.search(res.text)
        if m and ctx.agent(agent).get("can_escalate"):
            return False, f"{agent} asked to escalate: {m.group(1)}\n\n{clip(res.text, 4000)}"

        ok, check_output = run_checks(ctx, wt)
        if ok and commits_ahead(ctx, wt) > 0:
            return True, res.text
        if ok:
            check_output = "No commits relative to the base branch: nothing has been implemented yet."
    return False, f"Checks still failing after {ctx.limits['build_attempts']} attempts:\n\n{check_output}"


def build_with_escalation(ctx, issue, labels, branch, wt, log_dir, feedback="") -> bool:
    hard = ctx.labels["hard"] in labels
    order = [ctx.roles["senior"]] if hard else [ctx.roles["builder"], ctx.roles["senior"]]
    prior = ""
    for agent in order:
        ok, detail = build(ctx, agent, issue, branch, wt, log_dir, feedback, prior)
        if ok:
            return True
        prior = clip(detail, 8000)
        if agent != order[-1]:
            notify(ctx, f"#{issue['number']}: {agent} stuck, escalating to {order[-1]}")
    return False


# ---------------------------------------------------------------- review

JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_verdict(text: str) -> dict:
    for block in reversed(JSON_BLOCK_RE.findall(text)):
        try:
            v = json.loads(block)
        except json.JSONDecodeError:
            continue
        verdict = str(v.get("verdict", "")).upper()
        if verdict in ("APPROVE", "CHANGES"):
            return {"verdict": verdict, "summary": v.get("summary", ""),
                    "issues": v.get("issues") or []}
    m = re.search(r"VERDICT:\s*(APPROVE|CHANGES)", text, re.IGNORECASE)
    if m:
        return {"verdict": m.group(1).upper(), "summary": clip(text, 1500), "issues": []}
    return {"verdict": "CHANGES", "summary": "Reviewer output could not be parsed.",
            "issues": [{"severity": "major", "file": "", "detail": clip(text, 1500)}]}


def review(ctx, agent, issue, branch, wt, log_dir, rnd) -> dict:
    sha = head(wt)
    diff = git(wt, "diff", f"origin/{ctx.base}...HEAD").stdout
    vars_ = issue_vars(issue, branch) | {
        "round": str(rnd),
        "diff": clip(diff, ctx.limits["diff_chars"]),
    }
    res = run_agent(ctx, agent, ctx.cfg["review_prompts"][agent], vars_, wt, log_dir, f"review{rnd}")
    # Reviewers are read-only: undo any edits or commits they made.
    git(wt, "reset", "--hard", sha)
    git(wt, "clean", "-fd")
    return parse_verdict(res.text)


def format_review(agent: str, rnd: int, sha: str, v: dict) -> str:
    lines = [f"**{agent} review, round {rnd}** on `{sha[:8]}`: **{v['verdict']}**", "", v["summary"]]
    for i in v["issues"]:
        loc = f" `{i.get('file')}`" if i.get("file") else ""
        lines.append(f"- [{i.get('severity', '?')}]{loc} {i.get('detail', '')}")
    return "\n".join(lines)


def feedback_from(verdicts: dict) -> str:
    parts = []
    for agent, v in verdicts.items():
        if v["verdict"] != "CHANGES":
            continue
        parts.append(f"### From {agent}\n{v['summary']}")
        for i in v["issues"]:
            if i.get("severity") in ("blocker", "major"):
                loc = f" ({i.get('file')})" if i.get("file") else ""
                parts.append(f"- [{i['severity']}]{loc} {i.get('detail', '')}")
    return "\n".join(parts)


# ---------------------------------------------------------------- per-issue flow

def hand_to_human(ctx, issue_no, pr, reason, log_dir):
    L = ctx.labels
    msg = f"Handing off to a human: {reason}"
    if pr:
        post_comment(ctx, "pr", pr, msg, log_dir)
        gh(ctx, "pr", "edit", str(pr), "--add-label", L["needs_human"], check=False)
    post_comment(ctx, "issue", issue_no, msg, log_dir)
    gh(ctx, "issue", "edit", str(issue_no), "--add-label", L["needs_human"],
       "--remove-label", L["in_progress"], check=False)
    notify(ctx, f"#{issue_no} needs you: {reason}")
    return "needs-human"


def process_issue(ctx: Ctx, issue: dict) -> str:
    n = issue["number"]
    labels = {l["name"] for l in issue.get("labels", [])}
    branch = f"agent/{n}-{slugify(issue['title'])}"
    log_dir = ctx.logs / f"issue-{n}"
    log_dir.mkdir(parents=True, exist_ok=True)
    ctx.issue_tokens = 0
    L = ctx.labels
    pr = None

    notify(ctx, f"Starting #{n}: {issue['title']}")
    gh(ctx, "issue", "edit", str(n), "--add-label", L["in_progress"],
       "--remove-label", L["ready"], check=False)
    wt = make_worktree(ctx, branch)

    try:
        if not build_with_escalation(ctx, issue, labels, branch, wt, log_dir):
            return hand_to_human(ctx, n, None, "neither model could get the checks passing.", log_dir)

        push(wt, branch)
        existing = json.loads(gh(ctx, "pr", "list", "--head", branch, "--json", "number").stdout or "[]")
        if existing:
            pr = existing[0]["number"]
        else:
            body = (f"Closes #{n}\n\nOpened by the convo-ai orchestrator. Reviews from "
                    f"{', '.join(ctx.roles['reviewers'])} are posted as comments below.")
            url = gh(ctx, "pr", "create", "--head", branch, "--base", ctx.base,
                     "--title", f"{issue['title']} (#{n})", "--body", body).stdout.strip()
            pr = int(url.rstrip("/").rsplit("/", 1)[-1])

        for rnd in range(1, ctx.limits["review_rounds"] + 1):
            sha = head(wt)
            verdicts = {}
            for agent in ctx.roles["reviewers"]:
                verdicts[agent] = review(ctx, agent, issue, branch, wt, log_dir, rnd)
                post_comment(ctx, "pr", pr, format_review(agent, rnd, sha, verdicts[agent]), log_dir)

            if all(v["verdict"] == "APPROVE" for v in verdicts.values()):
                ok, ci_out = github_checks(ctx, pr)
                if ok:
                    remove_worktree(ctx, wt)
                    gh(ctx, "pr", "merge", str(pr), "--squash", "--delete-branch")
                    gh(ctx, "issue", "edit", str(n), "--remove-label", L["in_progress"], check=False)
                    notify(ctx, f"#{n} merged after {rnd} review round(s) "
                                f"({ctx.issue_tokens:,} metered tokens)")
                    return "merged"
                feedback = f"Both reviewers approved, but GitHub CI failed:\n```\n{clip(ci_out, 6000)}\n```"
            else:
                feedback = feedback_from(verdicts)

            if rnd == ctx.limits["review_rounds"]:
                break
            if not build_with_escalation(ctx, issue, labels, branch, wt, log_dir, feedback):
                return hand_to_human(ctx, n, pr, "could not address review feedback.", log_dir)
            push(wt, branch)

        return hand_to_human(ctx, n, pr,
                             f"no agreement after {ctx.limits['review_rounds']} review rounds.", log_dir)
    except BudgetExceeded as e:
        return hand_to_human(ctx, n, pr, str(e), log_dir)
    except Exception as e:  # keep the loop alive; leave the worktree for inspection
        return hand_to_human(ctx, n, pr, f"orchestrator error: {clip(str(e), 1500)}", log_dir)


# ---------------------------------------------------------------- main

def preflight(ctx: Ctx):
    for cmd in (["gh", "auth", "status"], ["hermes", "--version"], ["git", "--version"]):
        if run(cmd, check=False).returncode != 0:
            sys.exit(f"Preflight failed: `{' '.join(cmd)}` did not succeed.")
    if not (ctx.repo / ".git").exists():
        sys.exit(f"repo_path {ctx.repo} is not a git repository.")
    colors = {"ready": "0E8A16", "in_progress": "FBCA04", "hard": "B60205", "needs_human": "D93F0B"}
    for key, name in ctx.labels.items():
        gh(ctx, "label", "create", name, "--color", colors.get(key, "BFDADC"), "--force", check=False)


def main():
    ap = argparse.ArgumentParser(description="convo-ai Qwen/Claude build loop")
    ap.add_argument("--config", default=str(HERE / "orchestrator.yaml"))
    ap.add_argument("--issue", type=int, help="process one specific issue")
    ap.add_argument("--once", action="store_true", help="process one issue, then exit")
    ap.add_argument("--watch", action="store_true", help="keep polling for new issues")
    args = ap.parse_args()

    ctx = Ctx(Path(args.config))
    preflight(ctx)

    while True:
        issues = next_issues(ctx, args.issue)
        if not issues:
            if args.watch:
                time.sleep(ctx.cfg.get("poll_seconds", 300))
                continue
            print("No agent-ready issues.")
            return
        process_issue(ctx, issues[0])  # labels change, so re-list after each issue
        if args.once or args.issue:
            return


if __name__ == "__main__":
    main()
