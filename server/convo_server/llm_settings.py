"""Which model writes the summaries, changeable at run time (the hub's settings page).

``CONVO_LLM_MODEL`` in ``.env`` is the default. ``PUT /v1/settings/llm`` stores another
model in ``convo.db`` (``module_settings``), and the running worker uses it from the next
summary on; ``null`` goes back to the default.

Only the model name changes: the LLM server (``CONVO_LLM_BASE_URL``) stays what ``.env``
says, so a caller can't send transcripts to another host. A name the server doesn't list
(``GET /models``) is refused; when the server can't list its models (down, or no such
route) any name is taken, as llama-server with one model ignores the name anyway.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import Settings
from .llm import LLMClient
from .store import Store

log = logging.getLogger(__name__)

KEY = "llm_model"
MAX_NAME = 200
_NAME = re.compile(r"^[^\x00-\x1f\x7f]+$")


def apply_stored(llm: LLMClient, store: Store) -> None:
    """Use the stored model, if one is set (at start-up)."""
    stored = store.get_setting(KEY)
    if isinstance(stored, str) and stored:
        llm.model = stored
        log.info("summaries use %s (set in the hub's settings)", stored)


def describe(llm: LLMClient, store: Store, settings: Settings) -> dict[str, Any]:
    """``{"base_url", "model", "default_model", "override", "available"}``; ``available``
    is None when the LLM server can't list its models."""
    stored = store.get_setting(KEY)
    return {"base_url": llm.base_url, "model": llm.model, "default_model": settings.llm_model,
            "override": stored if isinstance(stored, str) and stored else None,
            "available": llm.models()}


def set_model(llm: LLMClient, store: Store, settings: Settings,
              model: str | None) -> dict[str, Any]:
    """Store ``model`` (None or "" = the ``.env`` default) and use it from now on.
    Raises ValueError for a name that isn't valid or that the server doesn't list."""
    name = (model or "").strip()
    if name and (len(name) > MAX_NAME or not _NAME.match(name)):
        raise ValueError("model must be a name of at most 200 characters")
    if name and name != settings.llm_model:
        available = llm.models()
        if available is not None and name not in available:
            listed = ", ".join(available) or "none"
            raise ValueError(f"the LLM server at {llm.base_url} has no model {name!r} "
                             f"(it lists: {listed})")
        store.set_setting(KEY, name)
        llm.model = name
    else:
        store.set_setting(KEY, None)
        llm.model = settings.llm_model
    log.info("summaries now use %s", llm.model)
    return describe(llm, store, settings)
