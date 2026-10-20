"""Dropping text whisper invents: stock phrases on noise, repetition loops, credits."""

from __future__ import annotations

import math
from pathlib import Path

import httpx
from conftest import tone

from convo_server.cli import build_transcriber
from convo_server.config import Settings
from convo_server.transcribe import (
    HallucinationFilter,
    WhisperCliTranscriber,
    WhisperServerTranscriber,
    collapse_repeats,
    mean_logprob,
    parse_cli_json,
    parse_verbose_json,
)

ON = HallucinationFilter()


def seg(text: str, p: float, start: float = 0.0, nsp: float = 0.0) -> dict:
    """A whisper-server verbose_json segment whose tokens all have probability ``p``."""
    words = [{"word": " " + w, "start": start, "end": start + 0.3, "probability": p}
             for w in text.split()]
    return {"id": 0, "text": " " + text, "start": start, "end": start + 1.0,
            "temperature": 0.0, "avg_logprob": math.log(p) / 2, "no_speech_prob": nsp,
            "tokens": list(range(len(words))), "words": words}


def texts(segments: list[dict], f: HallucinationFilter | None = ON) -> list[str]:
    return [p.text for p in parse_verbose_json({"segments": segments}, f)]


def test_unsure_stock_phrase_is_dropped_sure_one_kept():
    # turbo on 2 s of room noise: "Thank you." at a mean token log-prob of -0.7 to -0.9
    assert texts([seg("Thank you.", 0.4)]) == []
    assert texts([seg("Amen.", 0.3), seg("Bye.", 0.4)]) == []
    # said out loud, whisper is sure of it
    assert texts([seg("Thank you.", 0.95)]) == ["Thank you."]
    # unsure, but not a stock phrase: kept (dropping real words costs more)
    assert texts([seg("Okay.", 0.3), seg("Thank you for the slides.", 0.4)]) == [
        "Okay.", "Thank you for the slides."]


def test_threshold_and_switch():
    assert texts([seg("Thank you.", 0.6)], HallucinationFilter(phrase_logprob=-0.6)) == [
        "Thank you."]
    assert texts([seg("Thank you.", 0.4)], HallucinationFilter(enabled=False)) == [
        "Thank you."]
    assert texts([seg("Thank you.", 0.4)], None) == ["Thank you."]
    # no token probabilities (old server, plain -oj): nothing to judge, kept
    assert texts([{"start": 0, "end": 1, "text": " Thank you."}]) == ["Thank you."]


def test_mean_logprob_skips_blank_tokens():
    s = {"words": [{"word": " a", "probability": math.exp(-1)}, {"word": " ", "probability": 1e-9},
                   {"word": " b", "probability": math.exp(-3)}]}
    assert mean_logprob(s) == -2.0
    assert mean_logprob({"text": "x"}) is None


def test_subtitle_credits_and_punctuation_are_dropped():
    assert texts([seg("Subtitles by the Amara.org community", 0.9), seg(".", 0.2),
                  seg("...", 0.9), seg("Real words.", 0.9)]) == ["Real words."]


def test_repetition_loops_are_cut():
    assert collapse_repeats("the the the the the the the cat", 4) == "the the the the cat"
    loop = " ".join(["I think so."] * 9)
    assert collapse_repeats(loop, 4) == " ".join(["I think so."] * 4)
    assert collapse_repeats("no, no, no, that's okay", 4) == "no, no, no, that's okay"
    assert collapse_repeats("a b a b", 0) == "a b a b"
    assert texts([seg("We go. " * 12, 0.9)]) == ["We go. We go. We go. We go."]


def test_identical_segments_back_to_back_are_cut():
    loop = [seg("And then we left.", 0.9, start=i) for i in range(7)]
    assert texts(loop) == ["And then we left."] * 4
    # repeated by people, not in a run: kept
    assert texts([seg("Yeah.", 0.9), seg("No.", 0.9), seg("Yeah.", 0.9)]) == [
        "Yeah.", "No.", "Yeah."]


def test_whisper_server_applies_the_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"segments": [seg("Thank you.", 0.4),
                                                      seg("Hello there.", 0.9, 1.0)]})

    t = WhisperServerTranscriber("http://whisper.test", transport=httpx.MockTransport(handler))
    assert [p.text for p in t.transcribe(tone(300, 1))] == ["Hello there."]
    t.hallucinations = None
    assert [p.text for p in t.transcribe(tone(300, 1))] == ["Thank you.", "Hello there."]


def test_whisper_cli_full_json_gives_probabilities():
    t = WhisperCliTranscriber("whisper-cli", "model.bin")
    assert "-ojf" in t.command(Path("a.wav"), Path("out"))
    tok = [{"text": "[_BEG_]", "p": 0.9}, {"text": " Thank", "p": 0.4},
           {"text": " you", "p": 0.4}, {"text": ".", "p": 0.4}, {"text": "[_TT_100]", "p": 0.01}]
    payload = {"transcription": [
        {"offsets": {"from": 0, "to": 1000}, "text": " Thank you.", "tokens": tok},
        {"offsets": {"from": 1000, "to": 2000}, "text": " hello"}]}
    assert [p.text for p in parse_cli_json(payload, ON)] == ["hello"]
    assert len(parse_cli_json(payload)) == 2


def test_settings_configure_the_filter():
    base = {"ingest_token": "test-token-not-a-secret-00000", "whisper_server_url": "http://w"}
    t = build_transcriber(Settings(**base, whisper_drop_phrase_logprob=-0.2,
                                   whisper_max_repeats=2))
    assert t.hallucinations == HallucinationFilter(phrase_logprob=-0.2, max_repeats=2)
    off = build_transcriber(Settings(**base, whisper_drop_hallucinations=False))
    assert off.hallucinations is None


def test_cut_loop_keeps_no_word_times_kept_text_does():
    # Word times rebuild the text when a piece is split at a window cut (clips.py); a loop
    # the filter cut must not come back that way.
    loop = parse_verbose_json({"segments": [seg("go " * 12, 0.9)]}, ON)
    plain = parse_verbose_json({"segments": [seg("see you at noon", 0.9)]}, ON)
    assert loop[0].text.split().count("go") == 4 and loop[0].words == ()
    assert [w[2] for w in plain[0].words] == [" see", " you", " at", " noon"]


def test_whisper_server_retries_a_dropped_connection_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"segments": [seg("Hello there.", 0.9)]})

    t = WhisperServerTranscriber("http://whisper.test", transport=httpx.MockTransport(handler))
    assert [p.text for p in t.transcribe(tone(300, 1))] == ["Hello there."]

    def always(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    t = WhisperServerTranscriber("http://whisper.test", transport=httpx.MockTransport(always))
    try:
        t.transcribe(tone(300, 1))
    except httpx.RemoteProtocolError:
        pass
    else:
        raise AssertionError("a second drop must fail the request")
