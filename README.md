# Athena

Private conversation memory, and the first module of
[Odysseus](https://github.com/SDarioosh/Barvis) (our Odysseus fork), a personal, fully
local AI system.

Wearables record conversations. A server on the owner's machine transcribes them
(Silero VAD, whisper.cpp), identifies speakers, splits them into conversations,
summarizes them with a local LLM, stores everything in SQLite, and exposes it to Odysseus
through an MCP server. Nothing in the data path uses the cloud.

- Agent instructions: [AGENTS.md](AGENTS.md) (Claude Code: [CLAUDE.md](CLAUDE.md))
- Goal and plan: [MISSION.md](MISSION.md); status: [PROGRESS.md](PROGRESS.md)
- Server, API and MCP tools: [server/README.md](server/README.md)
- Watch app: [zepp-app/README.md](zepp-app/README.md)
- How Odysseus uses this module:
  [hub docs/fork/modules/odysseus-sound.md](https://github.com/SDarioosh/Barvis/blob/barvis/docs/fork/modules/odysseus-sound.md)
