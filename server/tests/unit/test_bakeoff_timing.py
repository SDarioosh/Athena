"""The bakeoff's first-visible-text timing and the streamed raw loop (no models)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BAKEOFF = Path(__file__).resolve().parents[2] / "eval" / "bakeoff.py"


@pytest.fixture(scope="module")
def bakeoff():
    spec = importlib.util.spec_from_file_location("bakeoff_eval", BAKEOFF)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bakeoff_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_visible_text_drops_reasoning(bakeoff):
    assert bakeoff.visible_text("<think>hm</think>The rent is 1520.") == "The rent is 1520."
    assert bakeoff.visible_text("<think>still thinking") == ""
    assert bakeoff.visible_text("Hello").strip() == "Hello"


def test_odysseus_first_text_skips_thinking_and_tools(bakeoff):
    first = bakeoff.FirstText(start=100.0)
    first.odysseus_event({"type": "turn_mode", "mode": "agent"}, now=100.5)
    first.odysseus_event({"delta": "planning", "thinking": True}, now=101.0)
    first.odysseus_event({"type": "tool_start", "tool": "search_transcripts"}, now=102.0)
    first.odysseus_event({"type": "final_response", "content": ""}, now=102.5)
    first.odysseus_event({"delta": "<think>x"}, now=103.0)
    first.odysseus_event({"delta": "</think>"}, now=103.5)
    assert first.seconds is None
    first.odysseus_event({"delta": " Alice"}, now=104.25)
    first.odysseus_event({"delta": " said"}, now=105.0)
    assert first.seconds == 4.25


def test_odysseus_final_response_counts_and_errors_do_not(bakeoff):
    first = bakeoff.FirstText(start=0.0)
    first.odysseus_event({"status": 500, "delta": "boom"}, now=1.0)
    first.odysseus_event({"type": "final_response", "content": "<think>a</think>Yes."},
                         now=2.0)
    assert first.seconds == 2.0


def test_hermes_text_or_result(bakeoff):
    first = bakeoff.FirstText(start=0.0)
    first.hermes_event({"type": "tool_use", "name": "mcp__convo__list_facts"}, now=1.0)
    first.hermes_event({"type": "result", "text": "Peanuts."}, now=3.0)
    assert first.seconds == 3.0
    first = bakeoff.FirstText(start=0.0)
    first.hermes_event({"type": "text", "text": "Pea"}, now=2.0)
    assert first.seconds == 2.0


def _sse(chunk: dict) -> str:
    return "data: " + json.dumps(chunk)


def test_chat_stream_rebuilds_message_with_tool_calls(bakeoff):
    first = bakeoff.FirstText(start=0.0)
    stream = bakeoff.ChatStream(first)
    lines = [
        ": keep-alive",
        _sse({"choices": [{"delta": {"role": "assistant", "content": None}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "search_", "arguments": ""}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "transcripts", "arguments": '{"query":'}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ' "rent"}'}}]}}]}),
        "data: [DONE]",
    ]
    for n, line in enumerate(lines):
        stream.feed(line, now=float(n))
    m = stream.message()
    assert m["content"] == ""
    assert m["tool_calls"] == [{"id": "call_1", "type": "function", "function": {
        "name": "search_transcripts", "arguments": '{"query": "rent"}'}}]
    assert stream.done and first.seconds is None


def test_chat_stream_content_and_first_token(bakeoff):
    first = bakeoff.FirstText(start=10.0)
    stream = bakeoff.ChatStream(first)
    stream.feed(_sse({"choices": [{"delta": {"content": ""}}]}), now=11.0)
    stream.feed(_sse({"choices": [{"delta": {"content": "Tom said "}}]}), now=12.5)
    stream.feed(_sse({"choices": [{"delta": {"content": "1520."}}]}), now=13.0)
    assert stream.message() == {"role": "assistant", "content": "Tom said 1520."}
    assert first.seconds == 2.5


def test_summary_line_medians(bakeoff):
    rows = [{"pass": True, "elapsed": 10.0, "first_text_s": 2.0},
            {"pass": False, "elapsed": 20.0, "first_text_s": None},
            {"pass": True, "elapsed": 30.0, "first_text_s": 4.0}]
    assert bakeoff.summary_line("odysseus", rows) == (
        "odysseus: 2/3 passed, median first text 3.0s (1 without answer text), "
        "median total 20.0s")
    assert "n/a" in bakeoff.summary_line("raw", [])
