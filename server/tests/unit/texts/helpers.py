"""Fictional text messages for the tests (555 numbers, made-up people)."""

from __future__ import annotations

from typing import Any

T0 = 1_760_000_000.0  # 2025-10-09 08:53 UTC
H = 3600.0
MIA, LEO, ZOE = "+15550100", "+15550111", "zoe@example.com"
MIA_CHAT, GROUP = "iMessage;-;+15550100", "iMessage;+;chat555"


def msg(guid: str, at: float, text: str = "hello", *, chat: str = MIA_CHAT,
        from_me: bool = False, handle: str | None = MIA, **extra: Any) -> dict[str, Any]:
    return {"id": guid, "chat": chat, "at": at, "from_me": from_me,
            "handle": None if from_me else handle, "text": text, **extra}


def upload(*messages: dict[str, Any], names: dict[str, str | None] | None = None,
           chats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    names = {MIA: "Mia Chen"} if names is None else names
    return {"source": "imessage",
            "handles": [{"handle": h, "name": n} for h, n in names.items()],
            "chats": chats if chats is not None else [
                {"id": MIA_CHAT, "name": None, "group": False, "handles": [MIA]}],
            "messages": list(messages)}


def thread_ids(store: Any) -> list[str]:
    return [r["conversation_id"] for r in store._q(
        "SELECT t.conversation_id FROM text_threads t JOIN conversations v"
        " ON v.id = t.conversation_id ORDER BY v.started_at")]


def lines(store: Any, conv_id: str) -> list[tuple[str, str]]:
    return [(s.speaker, s.text) for s in store.conversation_segments(conv_id)]
