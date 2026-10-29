"""Words whisper is nudged toward, set at run time (the hub's settings page).

The owner types names and jargon whisper keeps mishearing ("RokTalk, Meshtastic,
Rokland") as comma-separated text. ``PUT /v1/settings/vocabulary`` stores the list in
``convo.db`` (``module_settings``); :meth:`Pipeline.whisper_prompt` puts it in whisper's
prompt from the next recording on, after ``CONVO_WHISPER_PROMPT`` and before the names
of known speakers. An empty list goes back to no words.

Kept to a short list of words on purpose: in the evals a long or descriptive prompt
made whisper's timestamps and WER worse (server/README.md, "whisper gets a prompt").
"""

from __future__ import annotations

import re
from typing import Any

from .db.store import Store

KEY = "whisper_vocabulary"
MAX_WORDS = 50
MAX_WORD = 60
MAX_TEXT = 4000  # the CSV text as typed
_SPLIT = re.compile(r"[,;\n\r\t]+")
_BAD = re.compile(r"[\x00-\x1f\x7f]")


def parse(value: str | list[str] | None) -> list[str]:
    """Comma-separated text (or a list) to clean words: trimmed, quotes and a trailing
    period dropped, duplicates (any case) once. Raises ValueError past the limits."""
    if value is None:
        return []
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise ValueError(f"the word list is longer than {MAX_TEXT} characters")
        items = _SPLIT.split(value)
    else:
        if sum(len(str(v)) + 2 for v in value) > MAX_TEXT:
            raise ValueError(f"the word list is longer than {MAX_TEXT} characters")
        items = [p for v in value for p in _SPLIT.split(str(v))]
    words: list[str] = []
    seen: set[str] = set()
    for item in items:
        w = " ".join(item.split()).strip("\"'").strip().rstrip(".").strip()
        if not w:
            continue
        if _BAD.search(w):
            raise ValueError("words can't contain control characters")
        if len(w) > MAX_WORD:
            raise ValueError(f"{w[:20]!r}… is longer than {MAX_WORD} characters")
        if w.lower() not in seen:
            seen.add(w.lower())
            words.append(w)
    if len(words) > MAX_WORDS:
        raise ValueError(f"at most {MAX_WORDS} words (got {len(words)}): keep the list short, "
                         "a long prompt makes whisper worse")
    return words


def words(store: Store) -> list[str]:
    """The stored words ([] when none are set)."""
    stored = store.get_setting(KEY)
    return [w for w in stored if isinstance(w, str) and w] if isinstance(stored, list) else []


def describe(pipeline: Any) -> dict[str, Any]:
    """``{"words", "text", "max_words", "env_prompt", "prompt"}``: ``prompt`` is what
    whisper gets now (with known speakers' names)."""
    current = words(pipeline.store)
    return {"words": current, "text": ", ".join(current), "max_words": MAX_WORDS,
            "env_prompt": pipeline.settings.whisper_prompt.strip(),
            "prompt": pipeline.whisper_prompt()}


def set_words(pipeline: Any, value: str | list[str] | None) -> dict[str, Any]:
    """Store the words (empty: none) and use them from the next recording on."""
    cleaned = parse(value)
    pipeline.store.set_setting(KEY, cleaned or None)
    return describe(pipeline)
