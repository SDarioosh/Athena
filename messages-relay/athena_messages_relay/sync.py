"""One sync pass: what the server doesn't have yet, in batches.

Order of a pass: chats newly excluded are removed on the server, chats included again are
sent from the start, new messages are sent after the cursor, and (macOS 13+) edits and
unsends since the edit cursor. The cursor moves only after the server accepted the batch;
messages left out on purpose (filters, excluded chats, before ``since``) move it too.
Message text is never logged."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import SOURCE, filters
from . import state as state_mod
from .chatdb import Chat, ChatDB, Message
from .config import Config
from .contacts import Contacts, handle_key
from .upload import Uploader, UploadError

log = logging.getLogger(__name__)
MAX_TEXT = 4000


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def since_ts(since: Optional[str]) -> Optional[float]:
    """The start of ``since`` (YYYY-MM-DD) in the Mac's local time."""
    if not since:
        return None
    return datetime.strptime(since, "%Y-%m-%d").timestamp()


@dataclasses.dataclass
class Counts:
    sent: int = 0
    skipped: int = 0
    changed: int = 0
    chats_removed: int = 0
    batches: int = 0

    def __str__(self) -> str:
        return ("sent %d, left out %d, edits/unsends %d, chats removed %d, uploads %d"
                % (self.sent, self.skipped, self.changed, self.chats_removed, self.batches))


class Syncer:
    def __init__(self, cfg: Config, db: ChatDB, contacts: Contacts, uploader: Uploader,
                 state_file: Path) -> None:
        self.cfg = cfg
        self.db = db
        self.contacts = contacts
        self.uploader = uploader
        self.state_file = state_file
        self.state = state_mod.load(state_file)
        self.chats: Dict[int, Chat] = {}
        self.refresh_chats()

    # ---------------------------------------------------------------- chats
    def refresh_chats(self) -> None:
        self.chats = self.db.chats()

    def chat_name(self, chat: Chat) -> str:
        if chat.name:
            return chat.name
        names = [self.contacts.name_for(h) or h for h in chat.handles]
        return ", ".join(names) or chat.guid

    def excluded(self, chat: Chat) -> bool:
        if chat.group and not self.cfg.include_groups:
            return True
        return any(matches(chat, m, self.contacts) for m in self.cfg.exclude)

    # ---------------------------------------------------------------- passes
    def save(self) -> None:
        state_mod.save(self.state, self.state_file)

    def run_once(self) -> Counts:
        """One pass. Call it under ``state.locked``: the state is read again first, so a
        change the CLI made (``include``) is seen."""
        counts = Counts()
        self.state = state_mod.load(self.state_file)
        self.refresh_chats()
        self.remove_excluded(counts)
        self.send_again(counts)
        # Messages sent in this pass go in their current form, so only those of earlier
        # passes (ROWID <= sent_before) are checked for edits and unsends; afterwards the
        # edit cursor can move to the newest change seen when the pass began.
        sent_before = self.state.last_rowid
        edits_seen = self.db.edit_max() if self.db.has_edits else 0
        self.send_new(counts)
        self.send_changes(counts, sent_before)
        if edits_seen > self.state.edit_cursor:
            self.state.edit_cursor = edits_seen
            self.save()
        self.send_names(counts)
        return counts

    def remove_excluded(self, counts: Counts) -> None:
        done = set(self.state.deleted_chats)
        for chat in self.chats.values():
            if chat.guid in done or not self.excluded(chat):
                continue
            self.uploader.delete_chat(SOURCE, chat.guid)
            self.state.deleted_chats.append(chat.guid)
            self.state.resend.pop(chat.guid, None)
            self.save()
            counts.chats_removed += 1
            log.info("chat removed on the server (excluded): %s", chat.guid)

    def include_again(self, chat: Chat) -> None:
        """A chat no longer excluded: send its history again on the next pass."""
        if chat.guid in self.state.deleted_chats:
            self.state.deleted_chats.remove(chat.guid)
        self.state.resend[chat.guid] = 0
        self.save()

    def send_again(self, counts: Counts) -> None:
        by_guid = {c.guid: c for c in self.chats.values()}
        for guid in list(self.state.resend):
            chat = by_guid.get(guid)
            if chat is None or self.excluded(chat):
                self.state.resend.pop(guid, None)
                self.save()
                continue
            while True:
                rows = self.db.chat_messages_after(chat.rowid, self.state.resend[guid],
                                                   self.state.last_rowid, self.cfg.batch_size)
                if not rows:
                    break
                self._upload(rows, counts)
                self.state.resend[guid] = rows[-1].rowid
                self.save()
            self.state.resend.pop(guid, None)
            self.save()

    def send_new(self, counts: Counts) -> None:
        while True:
            rows = self.db.messages_after(self.state.last_rowid, self.cfg.batch_size)
            if not rows:
                return
            if any(r.chat_rowid is not None and r.chat_rowid not in self.chats for r in rows):
                self.refresh_chats()  # a chat started since the pass began
            self._upload(rows, counts)
            self.state.last_rowid = rows[-1].rowid
            self.save()

    def send_changes(self, counts: Counts, upto: int) -> None:
        if not self.db.has_edits or not upto:
            return
        while True:
            rows = self.db.changed_since(self.state.edit_cursor, upto, self.cfg.batch_size)
            if not rows:
                return
            n = self._upload(rows, counts, changes=True)
            counts.changed += n
            self.state.edit_cursor = max(r.raw_edit for r in rows)
            self.save()

    def send_names(self, counts: Counts) -> None:
        """Handles whose Contacts name changed since it was last sent."""
        changed = []
        for handle, sent in list(self.state.names.items()):
            name = self.contacts.name_for(handle)
            if (name or "") != sent:
                changed.append({"handle": handle, "name": name})
        if not changed:
            return
        for i in range(0, len(changed), 2000):
            part = changed[i:i + 2000]
            self.uploader.post({"source": SOURCE, "handles": part, "messages": []})
            counts.batches += 1
            for h in part:
                self.state.names[h["handle"]] = h["name"] or ""
            self.save()

    # ---------------------------------------------------------------- payload
    def _keep(self, m: Message, changes: bool) -> bool:
        if m.meta or m.chat_rowid is None or m.at is None:
            return False
        chat = self.chats.get(m.chat_rowid)
        if chat is None or self.excluded(chat):
            return False
        start = since_ts(self.cfg.since)
        if start is not None and (m.at or 0) < start:
            return False
        if m.deleted:
            return changes  # a new message already unsent: nothing to send
        if not m.text and not m.attachments:
            return False
        return filters.keep(m.text, m.handle, m.from_me)

    def _upload(self, rows: List[Message], counts: Counts, changes: bool = False) -> int:
        keep = [m for m in rows if self._keep(m, changes)]
        counts.skipped += len(rows) - len(keep)
        undecoded = sum(1 for m in keep if m.undecoded)
        if undecoded:
            log.warning("%d message(s) had text that could not be decoded", undecoded)
        return self._post(keep, counts)

    def _post(self, keep: List[Message], counts: Counts) -> int:
        """Upload ``keep``. If the server refuses the batch itself (400: a message it can't
        take), the batch is split until the refused message is found; that one is left
        out and logged (by id, never its text), so one odd message can't stall the relay.
        Any other refusal (a wrong token or URL) stops the pass."""
        if not keep:
            return 0
        payload = self.payload(keep)
        try:
            self.uploader.post(payload)
        except UploadError as e:
            if e.status != 400:
                raise
            if len(keep) == 1:
                log.warning("the server refused message %s, left out: %s", keep[0].guid, e)
                counts.skipped += 1
                return 0
            half = len(keep) // 2
            return self._post(keep[:half], counts) + self._post(keep[half:], counts)
        counts.batches += 1
        counts.sent += len(keep)
        for h in payload["handles"]:
            self.state.names[h["handle"]] = h["name"] or ""
        return len(keep)

    def payload(self, messages: List[Message]) -> Dict[str, Any]:
        chats = []
        handles: List[str] = []
        seen_chats: Set[int] = set()
        for m in messages:
            assert m.chat_rowid is not None
            if m.chat_rowid not in seen_chats:
                seen_chats.add(m.chat_rowid)
                chat = self.chats[m.chat_rowid]
                chats.append({"id": chat.guid, "name": chat.name, "group": chat.group,
                              "handles": chat.handles})
                handles += chat.handles
            if m.handle:
                handles.append(m.handle)
        out_handles = []
        for h in dict.fromkeys(handles):
            name = self.contacts.name_for(h)
            if h in self.state.names and self.state.names[h] == (name or ""):
                continue  # the server has this name already
            out_handles.append({"handle": h, "name": name})
        return {"source": SOURCE, "handles": out_handles, "chats": chats,
                "messages": [self.message(m) for m in messages]}

    def message(self, m: Message) -> Dict[str, Any]:
        text = "" if m.deleted else (m.text or "")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        return {"id": m.guid, "chat": self.chats[m.chat_rowid].guid,  # type: ignore[index]
                "at": iso(m.at), "from_me": m.from_me, "handle": m.handle, "text": text,
                "edited_at": iso(m.edited_at), "deleted": m.deleted, "reply_to": m.reply_to,
                "attachments": [] if m.deleted else m.attachments}


def matches(chat: Chat, match: str, contacts: Contacts) -> bool:
    """Whether an ``exclude`` entry names this chat: its id, its group name, or one of its
    participants by handle (phone numbers on their last 10 digits) or contact name."""
    m = match.strip().lower()
    if not m:
        return False
    if m == chat.guid.lower() or (chat.name and m == chat.name.lower()):
        return True
    key = handle_key(match)
    for h in chat.handles:
        if key and handle_key(h) == key:
            return True
        name = contacts.name_for(h)
        if name and name.lower() == m:
            return True
    return False
