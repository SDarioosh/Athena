---
trigger: always_on
description: Antigravity-specific notes for barvis-sound. Shared rules are in AGENTS.md.
---

# Antigravity notes

- `AGENTS.md` at the workspace root is the source of truth and is loaded automatically.
  Add shared rules there, not here.
- Terminal commands and browser actions never target the owner's real Odysseus install
  (Docker, `/home/steam/models/odysseus`, port 7000).
- When reviewing a PR, check that changes affecting answers, devices or deployment have a
  Server verification section (see AGENTS.md).
