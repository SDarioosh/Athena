"""Recognition: whisper prompt/fallback, voice samples, relabelling, speaker fixes."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import numpy as np
from conftest import tone
from test_pipeline import ingest

from convo_server.transcribe import (
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
    assert [p.text for p in t.transcribe(tone(300, 1), "Jenn, Shahyah.")] == ["hi"]
    body = seen[0]
    assert b'name="prompt"\r\n\r\nJenn, Shahyah.' in body
    assert b'name="temperature_inc"\r\n\r\n0.3' in body
    t.transcribe(tone(300, 1))
    assert b'name="prompt"' not in seen[1]


def test_whisper_cli_command_has_prompt_and_fallback():
    t = WhisperCliTranscriber("whisper-cli", "model.bin", temperature_inc=0.2)
    cmd = t.command(Path("a.wav"), Path("out"), "Barvis, Jenn.")
    assert cmd[cmd.index("-tpi") + 1] == "0.2"
    assert cmd[-2:] == ["--prompt", "Barvis, Jenn."]
    assert "--prompt" not in t.command(Path("a.wav"), Path("out"))


def test_non_speech_tags_are_dropped():
    pieces = [TranscriptPiece(0, 1, t) for t in
              ["[BLANK_AUDIO]", " (upbeat music) ", "*laughs*", "[MUSIC] [MUSIC]",
               "So [inaudible] then", "Real words."]]
    assert [p.text for p in _clean(pieces)] == ["So [inaudible] then", "Real words."]


def test_prompt_has_vocabulary_and_known_names(pipeline):
    pipeline.settings.whisper_prompt = "Barvis, Tailscale."
    pipeline.store.put_speaker("Jenn", np.ones(16, np.float32), is_owner=False)
    assert pipeline.whisper_prompt() == "Barvis, Tailscale. Jenn."
    ingest(pipeline, 0, np.concatenate([tone(200, 4), tone(600, 4)]))
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.transcriber.prompts == ["Barvis, Tailscale. Jenn."]


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


def test_loudest_borderline_cluster_is_the_owner():
    d = owner_diarizer()
    loud, quiet = unit(0.45, 0.893), unit(0.1, 0.995)  # owner scores 0.45 and 0.10
    labels, unnamed = d.name_clusters([quiet, loud], loudness=[-30.0, -18.0])
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
    call(mcp, "name_speaker", conversation_id=conv, speaker_label="Speaker 1", name="Jen")
    process_two_speakers(pipeline, session="s2", start=T0 + 3600)  # recognised as Jen
    assert "per-conversation" in call(mcp, "rename_speaker_everywhere",
                                      old_name="Speaker 1", new_name="Bob")
    out = call(mcp, "rename_speaker_everywhere", old_name="jen", new_name="Jenn")
    assert "2 lines across 2 conversations" in out
    assert [s["name"] for s in pipeline.store.speakers()] == ["Me", "Jenn"]
    assert "No lines by 'Jen'" in call(mcp, "rename_speaker_everywhere", old_name="Jen",
                                       new_name="X")

    transcript = call(mcp, "get_transcript", conversation_id=conv)
    line = int(re.search(r"#(\d+) \[\d+s [^\]]+\] Jenn:", transcript).group(1))
    out = call(mcp, "reassign_line", line_id=line, name="Me")
    assert "moved from Jenn to Me" in out
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
