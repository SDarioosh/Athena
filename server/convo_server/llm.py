"""Structured summaries and daily digests via llama-server (OpenAI-compatible API)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from .store import Segment

log = logging.getLogger(__name__)


class ActionItem(BaseModel):
    text: str = Field(description="What needs to be done, imperative, self-contained")
    owner: str | None = Field(default=None, description="Who should do it, if stated")
    due: str | None = Field(default=None, description="Deadline as stated, e.g. 'Friday'")
    at_seconds: int | None = Field(default=None,
                                   description="Offset in seconds where it was mentioned")


class Fact(BaseModel):
    subject: str = Field(description="Person, project or thing the fact is about")
    fact: str = Field(description="Durable fact worth remembering months from now")
    at_seconds: int | None = None


class ConversationSummary(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    action_items: list[ActionItem]
    people: list[str]
    topics: list[str]
    facts: list[Fact]


class DailyDigest(BaseModel):
    headline: str
    summary: str
    highlights: list[str]
    open_action_items: list[str]
    people: list[str]


SUMMARY_SYSTEM = """You turn conversation transcripts into structured notes for the \
user's private memory. Lines look like "[73] Speaker: text" where the number is the \
offset in seconds from the start of the conversation. "{owner}" is the user (the person \
wearing the recorder). Rules:
- Only state what the transcript supports; never invent names, dates or commitments.
- people: names of people who took part or were discussed (not "Speaker 1"); include \
"{owner}" only if they spoke.
- action_items: concrete commitments or requests. Set owner when clear.
- facts: durable, specific facts worth remembering for months (preferences, plans, \
relationships, decisions, numbers). Skip small talk and one-off logistics. Each fact is a \
complete sentence that names its subject and makes sense on its own, e.g. \
{{"subject": "Alice", "fact": "Alice is vegetarian."}} — never a bare value like "May".
- at_seconds: the bracketed number of the line that supports the item (copy it exactly).
- Use the transcript's language for content. Be concise. Output JSON only with keys: \
title, summary, key_points, action_items[{{text, owner, due, at_seconds}}], people, topics, \
facts[{{subject, fact, at_seconds}}]."""

MERGE_SYSTEM = """You merge partial structured notes of ONE long conversation (given in \
order) into a single set of notes with the same JSON shape. Deduplicate; keep at_seconds \
values as given. Output JSON only."""

DIGEST_SYSTEM = """You write a short daily digest of the user's conversations for {date} \
from their structured notes. Only use the given notes. Output JSON only."""


def format_transcript(segments: list[Segment], origin: float) -> str:
    lines = []
    for s in segments:
        off = max(0, int(s.started_at - origin))
        lines.append(f"[{off}] {s.speaker}: {s.text}")
    return "\n".join(lines)


_PLACEHOLDER = re.compile(r"^(speaker\s*\d+|unknown|user|the user)$", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 600.0, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=timeout, transport=transport)
        self.model = model

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   retries: int = 1) -> BaseModel:
        """Chat completion constrained to ``schema`` (llama-server json_schema grammar)."""
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                                "strict": True},
            },
            # Qwen3-style reasoning models: thinking adds latency and breaks JSON-only output
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last: Exception | None = None
        for _ in range(retries + 1):
            resp = self._http.post("/chat/completions", json=body)
            if resp.status_code >= 400:
                raise LLMError(f"llama-server HTTP {resp.status_code}: {resp.text[:300]}")
            content = resp.json()["choices"][0]["message"].get("content") or ""
            try:
                return schema.model_validate_json(_strip_fences(content))
            except ValidationError as e:
                log.warning("invalid structured output, retrying: %s", e)
                last = e
        raise LLMError(f"model did not return valid {schema.__name__}: {last}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


class Summarizer:
    def __init__(self, llm: LLMClient, owner_name: str = "Me",
                 max_chars_per_call: int = 120_000) -> None:
        self.llm = llm
        self.owner = owner_name
        self.max_chars = max_chars_per_call

    def summarize(self, segments: list[Segment]) -> dict[str, Any]:
        """Return a storage-ready dict (absolute ``source_at`` timestamps)."""
        if not segments:
            raise ValueError("no segments to summarize")
        origin = segments[0].started_at
        system = SUMMARY_SYSTEM.format(owner=self.owner)
        parts = self._split(segments, origin)
        if len(parts) == 1:
            result = self.llm.structured(system, parts[0], ConversationSummary)
        else:
            partials = [self.llm.structured(system, p, ConversationSummary).model_dump()
                        for p in parts]
            result = self.llm.structured(MERGE_SYSTEM, json.dumps(partials),
                                         ConversationSummary)
        assert isinstance(result, ConversationSummary)
        out = to_storage(result, origin)
        if not any(s.is_owner for s in segments):  # models tend to add the user anyway
            out["people"] = [p for p in out["people"] if p.lower() != self.owner.lower()]
        return out

    def _split(self, segments: list[Segment], origin: float) -> list[str]:
        parts: list[list[Segment]] = [[]]
        size = 0
        for s in segments:
            if size + len(s.text) > self.max_chars and parts[-1]:
                parts.append([])
                size = 0
            parts[-1].append(s)
            size += len(s.text) + 20
        return [format_transcript(p, origin) for p in parts]

    def digest(self, date: str, conversations: list[dict[str, Any]]) -> dict[str, Any]:
        notes = [{
            "time": datetime.fromtimestamp(c["started_at"], UTC).strftime("%H:%M UTC"),
            "title": c["title"], "summary": c["summary"], "key_points": c["key_points"],
            "people": c["people"],
            "action_items": [a["text"] for a in c.get("action_items", [])
                             if a.get("status", "open") == "open"],
        } for c in conversations]
        result = self.llm.structured(DIGEST_SYSTEM.format(date=date), json.dumps(notes),
                                     DailyDigest)
        out = result.model_dump()
        out["date"] = date
        out["conversation_ids"] = [c["id"] for c in conversations]
        return out


def to_storage(s: ConversationSummary, origin: float) -> dict[str, Any]:
    def at(x: int | None) -> float | None:
        return origin + x if x is not None and x >= 0 else None

    return {
        "title": s.title.strip(),
        "summary": s.summary.strip(),
        "key_points": [k.strip() for k in s.key_points if k.strip()],
        "topics": _dedupe(s.topics),
        "people": [p for p in _dedupe(s.people) if not _PLACEHOLDER.match(p)],
        "action_items": [{"text": a.text, "owner": a.owner, "due": a.due,
                          "source_at": at(a.at_seconds)} for a in s.action_items],
        "facts": [{"subject": f.subject, "fact": f.fact, "source_at": at(f.at_seconds)}
                  for f in s.facts],
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for i in items:
        k = i.strip()
        if k and k.lower() not in seen:
            seen[k.lower()] = k
    return list(seen.values())
