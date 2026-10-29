"""The llama-server client (OpenAI-compatible API): structured JSON calls."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 600.0, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=timeout, transport=transport)
        self.model = model

    @property
    def base_url(self) -> str:
        return str(self._http.base_url).rstrip("/")

    def models(self) -> list[str] | None:
        """The model ids the LLM server lists (``GET /models``), or None when it can't say
        (down, or no such route)."""
        try:
            resp = self._http.get("/models", timeout=5.0)
            data = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None
        ids = [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]
        return list(dict.fromkeys(ids))

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
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as e:  # down, timed out, connection reset
                raise LLMError(f"llama-server unreachable: {type(e).__name__}: {e}") from e
            if resp.status_code >= 400:
                raise LLMError(f"llama-server HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                content = resp.json()["choices"][0]["message"].get("content") or ""
            except (ValueError, LookupError, TypeError, AttributeError) as e:
                # a proxy's error page or an error object with HTTP 200
                raise LLMError(f"llama-server answer is not a chat completion: "
                               f"{resp.text[:300]}") from e
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
