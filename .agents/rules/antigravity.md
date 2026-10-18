---
trigger: always_on
description: Antigravity-specific notes for barvis-sound. Shared rules are in AGENTS.md.
---

# Antigravity notes

- `AGENTS.md` at the workspace root is the source of truth and is loaded automatically.
  Add shared rules there, not here.
- Terminal commands and browser actions never target the owner's real Odysseus install
  (Docker, `/home/steam/models/odysseus`, port 7000). Evals use the Barvis test instance
  on `127.0.0.1:7010`.
