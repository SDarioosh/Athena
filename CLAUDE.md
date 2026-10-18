@AGENTS.md

## Claude Code notes

- `AGENTS.md` above is the source of truth. Add shared rules there, not here.
- Run the checks from `AGENTS.md` (`ruff`, `pytest` in `server/`) before every push.
- `.claude/settings.json` blocks edits to the owner's Odysseus install, reading `.env` files and data directories, pushes to protected branches and `gh pr merge`, and asks before `docker`, `sudo`, `systemctl` and force pushes. Don't work around these rules (for example through a script); ask the owner instead.
