"""Recognition: whisper prompt/fallback, voice samples, relabelling, speaker fixes."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import numpy as np
import pytest
from conftest import tone
from test_pipeline import ingest

from convo_server.cli import build_transcriber
from convo_server.config import Settings
from convo_server.transcribe import (
    DecodeOptions,
    TranscriptPiece,
    WhisperCliTranscriber,
    WhisperServerTranscriber,
    _clean,
)


def test_whisper_server_gets_prompt_and_temperature_fallback():
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "text": "hi"}]})

    t = WhisperServerTranscriber("http://whisper.test", transport=httpx.MockTransport(handler),
                                 temperature_inc=0.3)
    assert [p.text for p in t.transcribe(tone(300, 1), "Kath, Marisol.")] == ["hi"]
    body = seen[0]
    assert b'name="prompt"\r\n\r\nKath, Marisol.' in body
    assert b'name="temperature_inc"\r\n\r\n0.3' in body
    assert _fields(body)["suppress_non_speech"] == "true"  # default: measured best
    t.transcribe(tone(300, 1))
    assert b'name="prompt"' not in seen[1]


def test_whisper_cli_command_has_prompt_and_fallback():
    t = WhisperCliTranscriber("whisper-cli", "model.bin", temperature_inc=0.2)
    cmd = t.command(Path("a.wav"), Path("out"), "Odysseus, Kath.")
    assert cmd[cmd.index("-tpi") + 1] == "0.2"
    assert cmd[-2:] == ["--prompt", "Odysseus, Kath."]
    assert "--prompt" not in t.command(Path("a.wav"), Path("out"))


def _fields(body: bytes) -> dict[str, str]:
    """Multipart form fields of a request body."""
    return {k.decode(): v.decode()
            for k, v in re.findall(rb'name="(\w+)"\r\n\r\n([^\r]*)', body)}


def test_whisper_server_gets_decoding_settings(tmp_path):
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"segments": []})

    settings = Settings(ingest_token="x" * 16, data_dir=tmp_path, language="auto",
                        whisper_server_url="http://whisper.test", whisper_beam_size=5,
                        whisper_best_of=4, whisper_no_speech_thold=0.5,
                        whisper_logprob_thold=-0.8, whisper_entropy_thold=2.6,
                        whisper_suppress_non_speech=True, whisper_temperature_inc=0.4,
                        whisper_carry_initial_prompt=True)
    t = build_transcriber(settings)
    assert isinstance(t, WhisperServerTranscriber)
    t._http._transport = httpx.MockTransport(handler)
    t.transcribe(tone(300, 1))
    f = _fields(seen[0])
    assert f["language"] == "auto"
    assert f["beam_size"] == "5" and f["best_of"] == "4"
    assert f["temperature"] == "0.0" and f["temperature_inc"] == "0.4"
    assert (f["no_speech_thold"], f["logprob_thold"], f["entropy_thold"]) == (
        "0.5", "-0.8", "2.6")
    assert f["suppress_non_speech"] == "true" and f["carry_initial_prompt"] == "true"


def test_whisper_cli_command_has_decoding_settings(tmp_path):
    settings = Settings(ingest_token="x" * 16, data_dir=tmp_path, whisper_cli="whisper-cli",
                        whisper_model="m.bin", whisper_beam_size=5, whisper_best_of=4,
                        whisper_no_speech_thold=0.5, whisper_suppress_non_speech=True)
    t = build_transcriber(settings)
    assert isinstance(t, WhisperCliTranscriber)
    cmd = t.command(Path("a.wav"), Path("out"))
    assert cmd[cmd.index("-bs") + 1] == "5" and cmd[cmd.index("-bo") + 1] == "4"
    assert cmd[cmd.index("-nth") + 1] == "0.5"
    assert "-sns" in cmd and "--carry-initial-prompt" not in cmd
    plain = WhisperCliTranscriber("whisper-cli", "m.bin", decode=DecodeOptions(
        suppress_non_speech=False, carry_initial_prompt=True)).command(Path("a"), Path("o"))
    assert "-sns" not in plain and "--carry-initial-prompt" in plain


def test_non_speech_tags_are_dropped():
    pieces = [TranscriptPiece(0, 1, t) for t in
              ["[BLANK_AUDIO]", " (upbeat music) ", "*laughs*", "[MUSIC] [MUSIC]",
               "So [inaudible] then", "Real words."]]
    assert [p.text for p in _clean(pieces)] == ["So [inaudible] then", "Real words."]


def test_prompt_has_vocabulary_and_known_names(pipeline):
    pipeline.settings.whisper_prompt = "Odysseus, Tailscale."
    pipeline.store.put_speaker("Kath", np.ones(16, np.float32), is_owner=False)
    assert pipeline.whisper_prompt() == "Odysseus, Tailscale. Kath."
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.transcriber.prompts == ["Odysseus, Tailscale. Kath."]


# ------------------------------------------------------------------ voice samples
def unit(*xs: float) -> np.ndarray:
    v = np.zeros(16, np.float32)
    v[: len(xs)] = xs
    return v / np.linalg.norm(v)


def test_voice_samples_are_kept_not_blended(tmp_path):
    from convo_server.diarize import load_voices
    from convo_server.store import Store

    store = Store(tmp_path / "c.db")
    store.add_voice_sample("Alice", unit(1, 0))
    store.add_voice_sample("Alice", unit(0, 1))  # e.g. Alice on the phone
    (alice,) = load_voices(store)
    assert alice.samples.shape == (2, 16)
    # a voice like either sample is a full match; the old 50/50 blend scored it 0.71
    assert alice.score(unit(1, 0)) > 0.99 and alice.score(unit(0, 1)) > 0.99
    assert float(unit(1, 0) @ alice.embedding) < 0.75

    # re-enrolling replaces enrollment samples and keeps learned ones
    store.put_speaker("Alice", unit(0, 0, 1), False, samples=[unit(0, 0, 1)] * 3)
    store.put_speaker("Alice", unit(0, 0, 1), False, samples=[unit(0, 0, 1)] * 2)
    assert load_voices(store)[0].samples.shape == (4, 16)


def test_old_single_voiceprints_become_samples(tmp_path):
    from convo_server.store import Store

    store = Store(tmp_path / "c.db")
    store.put_speaker("Me", unit(1), True)
    store._conn.execute("DELETE FROM voice_samples")
    store._conn.execute("PRAGMA user_version = 2")
    store.close()
    (me,) = Store(tmp_path / "c.db").speakers()
    assert me["samples"].shape == (1, 16) and me["is_owner"]


def test_enrollment_keeps_several_samples(tmp_path):
    from conftest import ToneEmbedder

    from convo_server.diarize import enroll_voice
    from convo_server.store import Store

    store = Store(tmp_path / "c.db")
    audio = np.concatenate([tone(200, 30), tone(300, 30)])  # 60 s, two "styles"
    assert enroll_voice(store, ToneEmbedder(), "Me", audio, is_owner=True) == 7
    (me,) = store.speakers()
    assert me["is_owner"] and me["samples"].shape[0] == 7


# --------------------------------------------------------------------- owner hint
def owner_diarizer(margin=0.10):
    from conftest import ToneEmbedder

    from convo_server.diarize import Diarizer, EnrolledVoice

    return Diarizer(ToneEmbedder(), [EnrolledVoice("Me", unit(1), True)],
                    match_threshold=0.5, owner_hint_margin=margin)


def test_loudest_borderline_cluster_is_the_owner(caplog):
    d = owner_diarizer()
    loud, quiet = unit(0.45, 0.893), unit(0.1, 0.995)  # owner scores 0.45 and 0.10
    with caplog.at_level("INFO", logger="convo_server.diarize"):
        labels, unnamed = d.name_clusters([quiet, loud], loudness=[-30.0, -18.0])
    assert "owner hint: speaker 2 of 2 named Me (voice score 0.45" in caplog.text
    assert "12.0 dB louder" in caplog.text
    assert [(x.name, x.is_owner) for x in labels] == [("Speaker 1", False), ("Me", True)]
    assert set(unnamed) == {"Speaker 1"}


def test_owner_hint_needs_loudness_gap_and_close_voice():
    loud, quiet = unit(0.45, 0.893), unit(0.1, 0.995)
    d = owner_diarizer()
    assert d.name_clusters([quiet, loud], [-20.0, -18.5])[0][1].name == "Speaker 2"
    far = unit(0.3, 0.954)  # 0.30: too far below the threshold
    assert d.name_clusters([quiet, far], [-30.0, -18.0])[0][1].name == "Speaker 2"
    assert owner_diarizer(margin=0).name_clusters(
        [quiet, loud], [-30.0, -18.0])[0][1].name == "Speaker 2"


def test_best_pairs_are_matched_first():
    from conftest import ToneEmbedder

    from convo_server.diarize import Diarizer, EnrolledVoice

    d = Diarizer(ToneEmbedder(), [EnrolledVoice("Alice", unit(1), False)])
    # the first cluster is a weak match, the second a strong one: Alice is the second
    labels, _ = d.name_clusters([unit(0.6, 0.8), unit(0.95, 0.31)])
    assert [x.name for x in labels] == ["Speaker 1", "Alice"]


# ----------------------------------------------------------------------- pyannote
def test_pyannote_backend_turns_off_telemetry_and_cloud():
    import os

    import pytest

    from convo_server import diarize_pyannote

    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "false"
    for model in ("pyannote/speaker-diarization-precision-2",
                  "pyannote/speaker-diarization-community-1-cloud"):
        with pytest.raises(ValueError, match="cloud"):
            diarize_pyannote._refuse_cloud(model)
    diarize_pyannote._refuse_cloud(diarize_pyannote.DEFAULT_MODEL)


def test_pyannote_diarizer_names_turns_with_enrolled_voices():
    from conftest import ToneEmbedder, silence

    from convo_server.diarize import EnrolledVoice, PieceRef
    from convo_server.diarize_pyannote import PyannoteDiarizer

    e = ToneEmbedder()
    run = np.concatenate([tone(200, 4), tone(600, 4), silence(1), tone(200, 3)])
    turns = [(0.0, 4.0, "SPEAKER_00"), (4.0, 8.0, "SPEAKER_01"), (9.0, 12.0, "SPEAKER_00")]
    d = PyannoteDiarizer(e, [EnrolledVoice("Me", e.embed(tone(200, 2)), True)],
                         segmenter=lambda audio: turns)
    pieces = [PieceRef(0, 0.2, 3.8), PieceRef(0, 4.1, 5.0), PieceRef(0, 5.0, 7.9),
              PieceRef(0, 9.5, 11.0), PieceRef(0, 8.2, 8.6)]  # last: between turns
    labels, unnamed = d.diarize_runs([run], pieces)
    assert [x.name for x in labels] == ["Me", "Speaker 1", "Speaker 1", "Me", "Speaker 1"]
    assert list(unnamed) == ["Speaker 1"]


def test_pyannote_merges_speakers_across_runs():
    from conftest import ToneEmbedder

    from convo_server.diarize import PieceRef
    from convo_server.diarize_pyannote import PyannoteDiarizer

    runs = [np.concatenate([tone(300, 3), tone(700, 3)]), tone(700, 3)]
    seg = {6.0: [(0.0, 3.0, "A"), (3.0, 6.0, "B")], 3.0: [(0.0, 3.0, "A")]}
    d = PyannoteDiarizer(ToneEmbedder(), [], segmenter=lambda a: seg[a.size / 16000])
    labels, _ = d.diarize_runs(runs, [PieceRef(0, 0, 3), PieceRef(0, 3, 6), PieceRef(1, 0, 3)])
    assert [x.name for x in labels] == ["Speaker 1", "Speaker 2", "Speaker 2"]


def test_pyannote_keeps_speakers_of_one_run_apart():
    """Two people on one body-worn mic can have similar voiceprints; pyannote's split
    inside a run must survive, so the second person doesn't take the first one's name."""
    from conftest import ToneEmbedder

    from convo_server.diarize import EnrolledVoice, PieceRef
    from convo_server.diarize_pyannote import PyannoteDiarizer

    e = ToneEmbedder()
    run = tone(300, 8)  # both "voices" embed the same
    turns = [(0.0, 4.0, "SPEAKER_00"), (4.0, 8.0, "SPEAKER_01")]
    d = PyannoteDiarizer(e, [EnrolledVoice("Tobias", e.embed(tone(300, 2)), False)],
                         segmenter=lambda audio: turns)
    labels, unnamed = d.diarize_runs([run], [PieceRef(0, 0.5, 3.5), PieceRef(0, 4.5, 7.5)])
    assert sorted(x.name for x in labels) == ["Speaker 1", "Tobias"]  # two people, not one
    assert list(unnamed) == ["Speaker 1"]


def test_cluster_cannot_link():
    from convo_server.diarize import cluster

    x = np.array([[1, 0], [1, 0], [1, 0]], dtype=np.float32)
    assert cluster(x, 0.5) == [0, 0, 0]
    assert cluster(x, 0.5, cannot_link=[0, 0, 1]) == [0, 1, 0]


# --------------------------------------------------------------- relabel & fixes
def test_naming_relabels_the_same_voice_in_other_conversations(pipeline):
    from test_mcp import T0, call, process_two_speakers

    from convo_server.mcp_server import build_mcp

    mcp = build_mcp(pipeline.store, pipeline.settings)
    process_two_speakers(pipeline)
    process_two_speakers(pipeline, session="s2", start=T0 + 3600)
    convs = sorted(pipeline.store.list_conversations(), key=lambda c: c["started_at"])
    assert len(convs) == 2
    assert pipeline.summarize_pending() == 0  # both already summarized
    out = call(mcp, "list_unnamed_speakers")
    assert out.count("Speaker 1 in") == 2 and "#" in out and "/view/" in out

    out = call(mcp, "name_speaker", conversation_id=convs[0]["id"], speaker_label="Speaker 1",
               name="Alice")
    assert "1 other conversation" in out
    later = pipeline.store.conversation_segments(convs[1]["id"])
    assert [s.speaker for s in later] == ["Me", "Alice"]
    assert pipeline.store.get_conversation(convs[1]["id"])["status"] == "pending"
    assert call(mcp, "list_unnamed_speakers") == "No unnamed speakers found."


def test_relabel_needs_a_confident_unique_match(pipeline):
    from conftest import ToneEmbedder
    from test_mcp import process_two_speakers

    from convo_server.speakers import relabel_matches

    store = pipeline.store
    process_two_speakers(pipeline)  # leaves an unnamed "Speaker 1" (600 Hz)
    voice = ToneEmbedder().embed(tone(600, 2))
    store.add_voice_sample("Alice", voice)
    store.add_voice_sample("Bea", voice + 0.05 * unit(0, 0, 1))  # sounds almost the same
    assert relabel_matches(store, "Alice", threshold=0.6) == []  # not unique enough
    assert relabel_matches(store, "Alice", threshold=0.6, margin=0.0)[0].name == "Alice"


def test_rename_everywhere_and_reassign_line(pipeline):
    from test_mcp import T0, call, process_two_speakers

    from convo_server.mcp_server import build_mcp

    mcp = build_mcp(pipeline.store, pipeline.settings)
    process_two_speakers(pipeline)
    conv = pipeline.store.list_conversations()[0]["id"]
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Kat")
    process_two_speakers(pipeline, session="s2", start=T0 + 3600)  # recognised as Kat
    assert "per-conversation" in call(mcp, "rename_speaker_everywhere",
                                      old_name="Speaker 1", new_name="Bob")
    out = call(mcp, "rename_speaker_everywhere", old_name="kat", new_name="Kath")
    assert "2 lines across 2 conversations" in out
    assert [s["name"] for s in pipeline.store.speakers()] == ["Me", "Kath"]
    assert "No lines by 'Kat'" in call(mcp, "rename_speaker_everywhere", old_name="Kat",
                                       new_name="X")

    transcript = call(mcp, "get_transcript", conversation_id=conv)
    line = int(re.search(r"#(\d+) \[\d+s\] Kath:", transcript).group(1))
    out = call(mcp, "reassign_line", line_id=line, name="Me")
    assert "moved from Kath to Me" in out
    seg = pipeline.store.get_segment(line)
    assert seg.speaker == "Me" and seg.is_owner
    assert "already" in call(mcp, "reassign_line", line_id=line, name="Me")
    assert "No line #999" in call(mcp, "reassign_line", line_id=999, name="Me")


def test_relabel_never_overwrites_a_name_a_person_set(pipeline):
    from test_mcp import T0, call, process_two_speakers

    from convo_server.mcp_server import build_mcp

    store = pipeline.store
    mcp = build_mcp(store, pipeline.settings)
    for i in range(3):  # three recordings with the same unnamed voice
        process_two_speakers(pipeline, session=f"s{i}", start=T0 + i * 3600)
    convs = sorted(store.list_conversations(), key=lambda c: c["started_at"])
    first = store.conversation_segments(convs[0]["id"])[1]
    call(mcp, "reassign_line", line_id=first.id, name="Dan")  # the user: that line was Dan
    owner_line = store.conversation_segments(convs[2]["id"])[0]
    call(mcp, "reassign_line", line_id=owner_line.id, name="Eve")
    out = call(mcp, "name_speaker", conversation_id=convs[1]["id"], speaker_label="Speaker 1",
               name="Carol")
    assert "matched 1 unnamed" in out  # s2's Speaker 1; s0 has no unnamed line left
    speakers = [[s.speaker for s in store.conversation_segments(c["id"])] for c in convs]
    assert speakers == [["Me", "Dan"], ["Me", "Carol"], ["Eve", "Carol"]]


def test_rename_everywhere_refuses_an_unnamed_label_as_new_name(pipeline):
    from test_mcp import call, process_two_speakers

    from convo_server.mcp_server import build_mcp

    mcp = build_mcp(pipeline.store, pipeline.settings)
    process_two_speakers(pipeline)
    out = call(mcp, "rename_speaker_everywhere", old_name="Me", new_name="Speaker 1")
    assert "not a name" in out
    assert {s.speaker for s in pipeline.store.session_segments("s1")} == {"Me", "Speaker 1"}
    assert [s["name"] for s in pipeline.store.speakers()] == ["Me"]


def test_pyannote_fallback_labels_dont_collide():
    from conftest import ToneEmbedder

    from convo_server.diarize import PieceRef
    from convo_server.diarize_pyannote import PyannoteDiarizer

    runs = [tone(300, 4), tone(900, 4)]

    def segmenter(audio):  # pyannote hears nobody in the second run
        return [(0.0, 4.0, "A")] if audio is runs[0] else []

    d = PyannoteDiarizer(ToneEmbedder(), [], segmenter=segmenter)
    labels, unnamed = d.diarize_runs(runs, [PieceRef(0, 0, 4), PieceRef(1, 0, 4)])
    assert [x.name for x in labels] == ["Speaker 1", "Speaker 2"]
    assert list(unnamed) == ["Speaker 1", "Speaker 2"]


def test_one_line_speakers_join_the_closest_speaker():
    from convo_server.diarize import absorb_singletons

    a, b = unit(1, 0, 0), unit(0, 1, 0)
    near_a = unit(1, 0.9, 0)  # one noisy piece, closer to a (0.74) than to b (0.67)
    far = unit(0, 0, 1)  # someone else entirely
    embs = np.stack([a, a, b, b, near_a, far])
    cids = [0, 0, 1, 1, 2, 3]
    assert absorb_singletons(embs, cids, 0.0) == cids  # off
    assert absorb_singletons(embs, cids, 0.3) == [0, 0, 1, 1, 0, 2]
    assert absorb_singletons(embs, cids, 0.8) == cids  # not similar enough
    assert absorb_singletons(embs[:2], [0, 1], 0.3) == [0, 1]  # nothing big to join


# ------------------------------------------------- eval/recognition.py (pure parts)
def _recognition():
    import importlib.util
    import sys

    if "recognition_eval" in sys.modules:
        return sys.modules["recognition_eval"]
    path = Path(__file__).resolve().parents[1] / "eval" / "recognition.py"
    spec = importlib.util.spec_from_file_location("recognition_eval", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["recognition_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_active_level_ignores_pauses_and_scaling_hits_the_target():
    rec = _recognition()
    from conftest import silence

    # a full-scale sine is -3 dBFS RMS; pauses must not pull the active level down
    x = np.concatenate([tone(300, 2, amp=0.1), silence(3), tone(300, 2, amp=0.1)])
    assert abs(rec.active_level_dbfs(x) - (20 * np.log10(0.1) - 3.01)) < 0.1
    assert rec.active_level_dbfs(silence(1)) == float("-inf")
    mask = np.zeros(x.size, bool)
    mask[: 2 * 16000] = True
    y = rec.scale_to_level(x, -30.0, mask)
    assert abs(rec.active_level_dbfs(y[mask]) + 30.0) < 0.05
    # two tracks at very different levels end up at the same level
    a, b = rec.scale_to_level(tone(200, 2, 0.5), -26), rec.scale_to_level(tone(500, 2, 0.01), -26)
    assert abs(rec.active_level_dbfs(a) - rec.active_level_dbfs(b)) < 0.05


def test_fixed_device_gain_puts_the_owner_at_minus_24_and_never_clips():
    rec = _recognition()
    owner = tone(300, 2, amp=0.01)  # about -43 dBFS
    out, gain_db = rec.device_gain(owner, rec.active_level_dbfs(owner))
    assert abs(rec.active_level_dbfs(out) + 24.0) < 0.1 and 18 < gain_db < 20
    # the same fixed gain on a much louder burst: the limiter holds it under -1 dBFS
    loud = np.concatenate([owner, tone(300, 1, amp=0.5), owner])
    out, _ = rec.device_gain(loud, rec.active_level_dbfs(owner))
    assert np.max(np.abs(out)) <= 10 ** (-1 / 20) + 1e-4
    assert abs(rec.active_level_dbfs(out[: owner.size]) + 24.0) < 0.1  # before the burst
    assert rec.limiter(owner) is not owner and np.allclose(rec.limiter(owner), owner)


def test_owner_and_others_wer_split():
    pytest.importorskip("jiwer")
    rec = _recognition()
    turns = [(0.0, 2.0, "OWN"), (2.0, 4.0, "B"), (4.0, 6.0, "C")]
    ref = {"OWN": [(0.1, 0.5, "Hello"), (0.8, 1.2, "there.")],
           "B": [(2.2, 2.6, "how"), (2.9, 3.2, "are"), (3.5, 3.9, "you?"),
                 (3.0, 3.1, "um")],  # a filler: not a word for WER
           "C": [(4.2, 4.6, "fine")]}
    # one hypothesis segment spans the owner and B; B's "are" is misheard as "art"
    segs = [(0.0, 4.0, "Me", "Hello there, how art you?"), (4.1, 5.5, "Speaker 1", "fine")]
    words = rec.assign_words(segs, turns, ref_words=ref)
    assert words == [("OWN", "hello"), ("OWN", "there"), ("B", "how"), ("B", "art"),
                     ("B", "you"), ("C", "fine")]
    out = rec.split_wer(ref, words, "OWN")
    assert out["wer_owner"] == 0 and out["owner_ref_words"] == 2
    assert out["others_ref_words"] == 4 and out["wer_others"] == 0.25
    # the window starts at 100 s: segments are relative to it, turns absolute
    moved = [(a + 100, b + 100, s) for a, b, s in turns]
    moved_ref = {k: [(a + 100, b + 100, w) for a, b, w in v] for k, v in ref.items()}
    assert rec.assign_words(segs, moved, 100.0, moved_ref) == words
    # an inserted word counts for the speaker talking then; a dropped one for its speaker
    out = rec.split_wer(ref, [("OWN", "yeah"), *words[:-1]], "OWN")  # + yeah, - fine
    assert (out["owner_errors"], out["others_errors"]) == (1, 2)
    # someone's "okay" inside another speaker's long turn goes to whoever said it
    long_turns = [(0.0, 6.0, "B"), (2.0, 2.6, "OWN")]
    ref2 = {"B": [(0.2, 1.8, "so the remote"), (3.0, 5.8, "needs a big button")],
            "OWN": [(2.0, 2.6, "okay")]}
    w2 = rec.assign_words([(0.0, 6.0, "x", "so the remote okay needs a big button")],
                          long_turns, ref_words=ref2)
    assert ("OWN", "okay") in w2
    assert rec.split_wer(ref2, w2, "OWN")["wer_owner"] == 0
    # nothing said by the owner: no owner WER, the owner's words count as insertions
    out = rec.split_wer({"B": ref["B"]}, words, "OWN")
    assert out["wer_owner"] is None and out["owner_errors"] == 2


def test_missed_speech_per_group():
    rec = _recognition()
    turns = [(10.0, 14.0, "OWN"), (14.0, 18.0, "B"), (18.0, 20.0, "C")]
    segs = [(0.0, 4.0, "Me", "x"), (4.0, 6.0, "Speaker 1", "y")]  # window starts at 10
    out = rec.missed_by_group(turns, segs, "OWN", 10.0, 20.0)
    assert out["missed_owner"] == 0.0
    assert abs(out["others_speech_s"] - 6.0) < 1e-9 and abs(out["missed_others"] - 4 / 6) < 1e-9


def test_speech_masks_separate_owner_and_others():
    rec = _recognition()
    turns = [(0.0, 1.0, "OWN"), (0.5, 2.0, "B")]  # 0.5-1.0 is overlap
    m = rec.speech_masks(turns, "OWN", 3 * 16000)
    assert m["owner"].sum() == 8000 and m["others"].sum() == 16000
    assert m["speech"].sum() == 32000 and m["solo:B"].sum() == 16000


def test_gate_keeps_only_the_speakers_turns():
    rec = _recognition()
    x = np.ones(10 * 16000, np.float32)
    y = rec.gate_to_turns(x, [(2.0, 3.0, "A"), (6.0, 7.0, "B")], "A", pad=0.25, ramp=0.02)
    assert y[int(2.5 * 16000)] == 1.0 and y[int(1.8 * 16000)] == 1.0  # turn and pad
    assert y[int(1.5 * 16000)] == 0.0 and y[int(6.5 * 16000)] == 0.0  # outside, B's turn
    edge = y[int(1.75 * 16000) - 160: int(1.75 * 16000) + 160]
    assert np.all(np.diff(edge) >= 0) and 0.4 < edge[160] < 0.6  # a ramp, not a click


def test_sim_placement_is_deterministic_and_inside_the_room():
    rec = _recognition()
    mic = np.asarray(rec.SIM_MIC)
    for d in (1.0, 2.0, 3.0):
        owner, others = rec.place_sources(3, d)
        owner2, others2 = rec.place_sources(3, d)
        assert np.array_equal(owner, owner2)
        assert all(np.array_equal(a, b) for a, b in zip(others, others2, strict=True))
        assert abs(np.linalg.norm(owner - mic) - 0.2) < 1e-9
        for p in others:
            assert abs(np.linalg.norm(p - mic) - d) < 1e-6
            assert np.all(p >= 0.3) and np.all(p <= np.asarray(rec.SIM_ROOM) - 0.3)
        assert len({tuple(np.round(p, 6)) for p in others}) == 3  # spread, not stacked
    other_seed = rec.place_sources(3, 2.0, seed=1)[1]
    assert any(not np.array_equal(a, b)
               for a, b in zip(other_seed, rec.place_sources(3, 2.0)[1], strict=True))
    assert rec.place_sources(0, 2.0)[1] == []


def test_opus_round_trip_keeps_length():
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or b"libopus" not in subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True).stdout:
        pytest.skip("ffmpeg with libopus not installed")
    rec = _recognition()
    rng = np.random.default_rng(0)
    x = (tone(440, 1.5, 0.2) + 0.01 * rng.standard_normal(24000)).astype(np.float32)
    y = rec.opus_roundtrip(x, 16)
    assert y.dtype == np.float32 and y.size == x.size and np.all(np.isfinite(y))
    # still the same signal (aligned, no level change worth mentioning)
    assert np.corrcoef(x[1600:-1600], y[1600:-1600])[0, 1] > 0.9
    assert abs(rec.active_level_dbfs(y) - rec.active_level_dbfs(x)) < 1.5


def test_owner_precision_and_recall():
    rec = _recognition()
    turns = [(0.0, 4.0, "OWN"), (4.0, 8.0, "B"), (8.0, 10.0, "C")]
    # 0-3 s owner as owner; 3-6 s labelled owner (1 s owner, 2 s B); 6-8 B as B; 8-10 none
    segs = [(0.0, 3.0, "OWN", "a"), (3.0, 6.0, "OWN", "b"), (6.0, 8.0, "B", "c")]
    out = rec._naming(turns, 0.0, 10.0, segs, ["OWN", "B"])
    assert abs(out["owner_precision"] - 4 / 6) < 0.02  # 4 s of the 6 s labelled owner
    assert abs(out["owner_recall"] - 1.0) < 0.02  # all 4 s of the owner
    assert out["owner_frames"] == 40
    pooled = rec.pooled([{**out, "ref_words": 1, "substitutions": 0, "deletions": 0,
                          "insertions": 0, "total": 1.0, "missed": 0.0,
                          "false_alarm": 0.0, "confusion": 0.0, "seconds_to_process": 1}])
    assert abs(pooled["owner_precision"] - 4 / 6) < 0.02


def test_late_joiners_are_cut_from_the_reference():
    rec = _recognition()
    mutes = rec.late_joiner_mutes(["OWN", "B", "C", "D"], 100.0, 200.0)
    assert mutes == (("C", 100.0, 150.0), ("D", 100.0, 150.0))
    turns = [(90.0, 120.0, "C"), (140.0, 160.0, "C"), (110.0, 130.0, "OWN")]
    assert rec.cut_turns(turns, mutes) == [(90.0, 100.0, "C"), (110.0, 130.0, "OWN"),
                                           (150.0, 160.0, "C")]
    words = {"C": [(120.0, 120.3, "early"), (155.0, 155.3, "late")],
             "OWN": [(120.0, 120.3, "mine")]}
    assert rec.cut_words(words, mutes) == {"C": [(155.0, 155.3, "late")],
                                           "OWN": [(120.0, 120.3, "mine")]}


def test_owner_dips_turn_the_voice_down_in_stretches():
    rec = _recognition()
    env = rec.owner_dips(120 * 16000, 12.0)
    assert env.max() == 1.0 and 10 ** (-12 / 20) - 1e-6 <= env.min() < 10 ** (-6 / 20)
    down = (env < 0.99).mean()
    assert 0.1 < down < 0.6  # dips of 2-8 s every 5-20 s
    assert np.array_equal(env, rec.owner_dips(120 * 16000, 12.0))  # deterministic


def test_unenrolled_speakers_given_an_enrolled_name():
    rec = _recognition()
    turns = [(0.0, 4.0, "OWN"), (4.0, 8.0, "NEW")]
    segs = [(0.0, 4.0, "OWN", "a"), (4.0, 6.0, "OWN", "b"), (6.0, 8.0, "Speaker 1", "c")]
    out = rec._naming(turns, 0.0, 8.0, segs, ["OWN"])
    assert abs(out["unenrolled_named"] - 0.5) < 0.03  # 2 s of NEW's 4 s under "OWN"
