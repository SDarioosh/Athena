"""The owner's Odysseus checklists for the T5's Tasks tab (docs/t5-pda-plan.md §4.4).

- Read: `GET /api/codex/todos?format=json` -> `{"notes": [{id, title, type, label, pinned,
  due_date, updated_at, items: [{text, done}], content}]}` (scope todos:read). A text answer
  (a hub without `format=json`) or 403 (a token without todos:read) means "no todos":
  `notes()` returns None and logs why. Any other failure is an `UploadError`.
- Toggle: `POST /api/codex/todos` `{"action": "toggle_item", "note_id", "index", "done"}`
  (scope todos:write) -> HTTP 200 with `{"exit_code": 0 | 1, ...}`.

Same token, URL and request rules as the calendar (`OdysseusCalendar`, `Uploader`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .uploader import RETRY, Uploader, UploadError

log = logging.getLogger("barvis_relay.pda")

TODOS = "/api/codex/todos"


class Todos(Protocol):
    def notes(self) -> list[dict[str, Any]] | None:
        """The notes in the hub's order, or None if this hub/token can't list them."""

    def toggle(self, note_id: str, index: int, done: bool) -> int:
        """Set item `index` of the note to `done`; return the hub's `exit_code`."""


class OdysseusTodos(Uploader):
    def __repr__(self) -> str:
        return f"OdysseusTodos({self.server!r})"

    def notes(self) -> list[dict[str, Any]] | None:
        try:
            answer = self._request("GET", f"{TODOS}?format=json")
        except UploadError as e:
            if e.status == 403:
                log.warning("Odysseus token can't read todos (needs the todos:read scope); "
                            "the feed has no todos")
                return None
            if e.status == 200:  # 200 but not a JSON object: the old text answer
                log.warning("Odysseus answers todos as text (a hub without format=json); "
                            "the feed has no todos")
                return None
            raise
        notes = answer.get("notes")
        if not isinstance(notes, list):
            log.warning("Odysseus todos answer has no notes list (an older hub?); "
                        "the feed has no todos")
            return None
        return [n for n in notes if isinstance(n, dict)]

    def toggle(self, note_id: str, index: int, done: bool) -> int:
        body = {"action": "toggle_item", "note_id": note_id, "index": index, "done": done}
        answer = self._request("POST", TODOS, body=json.dumps(body).encode(),
                               content_type="application/json")
        code = answer.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise UploadError(RETRY, "todos toggle answered without exit_code", 200)
        if code != 0:
            log.warning("Odysseus refused the toggle: %s", str(answer.get("error"))[:200])
        return code
