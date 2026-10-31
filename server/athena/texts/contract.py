"""The upload of text messages (``POST /v1/ingest/messages``) and its checks.

``parse_upload`` turns the JSON body into an ``Upload`` or raises ``ValueError`` naming
the first bad field; nothing of a refused upload is stored. The shape is part of the
contract with the messages relay (docs/messages.md, "Upload contract").
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..timeutil import parse_instant
from ..validation import MAX_LINE_CHARS

SOURCES = ("imessage", "sms", "whatsapp", "signal")
MAX_MESSAGES = 1_000
MAX_CHATS = 1_000
MAX_HANDLES = 2_000
MAX_ATTACHMENTS = 20
MAX_NAME_CHARS = 200

# chat and message ids as the sources write them: "iMessage;-;+15550100",
# "iMessage;+;chat123456789", GUIDs; URL-encoded ones are accepted as sent
ID = re.compile(r"^[A-Za-z0-9;:+@._%=-]{1,200}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Handle:
    handle: str
    name: str | None  # in the owner's contacts; None = no contact


@dataclass(frozen=True)
class Chat:
    key: str
    name: str | None
    group: bool
    handles: tuple[str, ...]


@dataclass(frozen=True)
class Attachment:
    name: str
    type: str


@dataclass(frozen=True)
class Message:
    guid: str
    chat: str
    at: float
    from_me: bool
    handle: str | None
    text: str
    edited_at: float | None
    deleted: bool
    reply_to: str | None
    attachments: tuple[Attachment, ...] = ()

    @property
    def line(self) -> str:
        """The transcript line: the text, else what the attachments are; '' for neither."""
        if self.text:
            return self.text
        return " ".join(attachment_word(a) for a in self.attachments)

    @property
    def text_hash(self) -> str:
        """What a resend compares: the text and the attachments as sent."""
        return hashlib.sha256(json.dumps(
            [self.text, [[a.name, a.type] for a in self.attachments]],
            ensure_ascii=False).encode()).hexdigest()


@dataclass
class Upload:
    source: str
    handles: list[Handle] = field(default_factory=list)
    chats: list[Chat] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)


def attachment_word(a: Attachment) -> str:
    kind = (a.type or "").split("/", 1)[0].lower()
    if kind == "image":
        return "[photo]"
    if kind in ("video", "audio"):
        return f"[{kind}]"
    return f"[file: {a.name}]" if a.name else "[file]"


# ------------------------------------------------------------------ checks
def _list(data: dict[str, Any], key: str, limit: int) -> list[Any]:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    if len(value) > limit:
        raise ValueError(f"at most {limit} {key} per upload")
    return value


def _obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID.match(value):
        raise ValueError(f"{where} must match {ID.pattern}")
    return value


def _handle(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME_CHARS \
            or _CONTROL.search(value):
        raise ValueError(f"{where} must be a phone number or email (at most "
                         f"{MAX_NAME_CHARS} characters)")
    return value.strip()


def _name(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_NAME_CHARS:
        raise ValueError(f"{where} must be a string of at most {MAX_NAME_CHARS} characters")
    name = _SPACE.sub(" ", _CONTROL.sub("", value)).strip()
    return name or None


def _bool(value: Any, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false")
    return value


def _time(value: Any, where: str, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    try:
        if value is None or isinstance(value, bool):
            raise ValueError("no time")
        return parse_instant(str(value))
    except (ValueError, TypeError) as e:
        raise ValueError(f"{where} must be ISO-8601 or epoch seconds") from e


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    if len(value) > MAX_LINE_CHARS:
        raise ValueError(f"{where} is longer than {MAX_LINE_CHARS} characters")
    # one line per message: newlines inside a message become spaces
    return _SPACE.sub(" ", _CONTROL.sub("", value)).strip()


def parse_upload(data: Any) -> Upload:
    """The upload in ``data`` (the decoded JSON body), checked; ValueError otherwise."""
    data = _obj(data, "the body")
    source = data.get("source")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    up = Upload(source=source)
    for i, h in enumerate(_list(data, "handles", MAX_HANDLES)):
        h = _obj(h, f"handles[{i}]")
        up.handles.append(Handle(_handle(h.get("handle"), f"handles[{i}].handle"),
                                 _name(h.get("name"), f"handles[{i}].name")))
    for i, ch in enumerate(_list(data, "chats", MAX_CHATS)):
        ch = _obj(ch, f"chats[{i}]")
        hs = ch.get("handles") or []
        if not isinstance(hs, list) or len(hs) > 500:
            raise ValueError(f"chats[{i}].handles must be a list of at most 500")
        up.chats.append(Chat(_id(ch.get("id"), f"chats[{i}].id"),
                             _name(ch.get("name"), f"chats[{i}].name"),
                             _bool(ch.get("group"), f"chats[{i}].group"),
                             tuple(dict.fromkeys(_handle(x, f"chats[{i}].handles")
                                                 for x in hs))))
    for i, m in enumerate(_list(data, "messages", MAX_MESSAGES)):
        where = f"messages[{i}]"
        m = _obj(m, where)
        from_me = _bool(m.get("from_me"), f"{where}.from_me")
        handle = m.get("handle")
        if handle is not None:
            handle = _handle(handle, f"{where}.handle")
        if not from_me and handle is None:
            raise ValueError(f"{where}.handle is needed when from_me is false")
        atts = m.get("attachments") or []
        if not isinstance(atts, list) or len(atts) > MAX_ATTACHMENTS:
            raise ValueError(f"{where}.attachments must be a list of at most "
                             f"{MAX_ATTACHMENTS}")
        reply_to = m.get("reply_to")
        up.messages.append(Message(
            guid=_id(m.get("id"), f"{where}.id"),
            chat=_id(m.get("chat"), f"{where}.chat"),
            at=_time(m.get("at"), f"{where}.at"),  # type: ignore[arg-type]
            from_me=from_me, handle=None if from_me else handle,
            text=_text(m.get("text"), f"{where}.text"),
            edited_at=_time(m.get("edited_at"), f"{where}.edited_at", optional=True),
            deleted=_bool(m.get("deleted"), f"{where}.deleted"),
            reply_to=None if reply_to is None else _id(reply_to, f"{where}.reply_to"),
            attachments=tuple(
                Attachment(_name(_obj(a, f"{where}.attachments").get("name"),
                                 f"{where}.attachments.name") or "",
                           _name(a.get("type"), f"{where}.attachments.type") or "")
                for a in atts)))
    return up
