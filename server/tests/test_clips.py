"""Cutting speech into whisper clips: packing, cut gaps, time mapping, prompt carry-over."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import dominant_freq, silence, tone, wav

from convo_server.audio import SAMPLE_RATE
from convo_server.clips import Window, plan_windows, tail_words
from convo_server.diarize import SpeakerLabel
from convo_server.transcribe import TranscriptPiece
from convo_server.vad import Span

T0 = 1_760_000_000.0


def test_window_zero_sends_each_padded_region_alone():
    ws = plan_windows([Span(1, 3), Span(8, 10)], 30, pad=0.25, max_seconds=0, max_gap=1)
    assert ws == [Window(((0.75, 3.25),)), Window(((7.75, 10.25),))]


def test_padding_stays_in_run_and_does_not_overlap_neighbours():
    ws = plan_windows([Span(0.2, 3), Span(4, 5), Span(9, 9.9)], 10, pad=1.0, max_seconds=0,
                      max_gap=1)
    # 0.2-3 and 4-5 padded by 1 s meet: one interval, the shared second not sent twice
    assert ws == [Window(((0.0, 6.0),)), Window(((8.0, 10.0),))]


def test_packing_cuts_long_gaps_and_respects_max_length():
    regions = [Span(1, 3), Span(8, 10), Span(20, 22), Span(23.5, 25)]
    ws = plan_windows(regions, 30, pad=0.25, max_seconds=8, max_gap=1.0)
    assert all(w.duration <= 8 + 1e-9 for w in ws)
    # 0.75-3.25 and 7.75-10.25: 4.5 s gap shortened to 1 s, half on each side
    assert ws[0] == Window(((0.75, 3.75), (7.25, 10.25)))
    assert ws[0].duration == pytest.approx(6.0)
    # 19.75-22.25 and 23.25-25.25: 1 s gap kept as is -> one contiguous slice
    assert ws[1] == Window(((19.75, 25.25),))
    # every padded region is in exactly one window
    covered = [p for w in ws for p in w.parts]
    assert covered == sorted(covered)


def test_long_region_gets_its_own_window():
    ws = plan_windows([Span(1, 2), Span(4, 50), Span(52, 53)], 60, pad=0.25, max_seconds=20,
                      max_gap=1.0)
    assert [w.parts for w in ws] == [((0.75, 2.25),), ((3.75, 50.25),), ((51.75, 53.25),)]


def test_gaps_kept_whole_with_large_max_gap():
    ws = plan_windows([Span(1, 3), Span(8, 10)], 30, pad=0.25, max_seconds=28, max_gap=99)
    assert ws == [Window(((0.75, 10.25),))]


def test_to_run_maps_across_cuts():
    w = Window(((0.75, 3.75), (7.25, 10.25)))  # clip 0-3 -> run 0.75-3.75, 3-6 -> 7.25-10.25
    assert w.to_run(0.0) == pytest.approx(0.75)
    assert w.to_run(2.0) == pytest.approx(2.75)
    assert w.to_run(3.0) == pytest.approx(7.25)  # a start at the cut: after it
    assert w.to_run(3.0, end=True) == pytest.approx(3.75)  # an end at the cut: before it
    assert w.to_run(4.5) == pytest.approx(8.75)
    assert w.to_run(99.0, end=True) == pytest.approx(10.25)  # clamped to the clip
    audio = np.arange(11 * SAMPLE_RATE, dtype=np.float32)
    clip = w.extract(audio)
    assert clip.size == 6 * SAMPLE_RATE
    assert clip[3 * SAMPLE_RATE] == audio[int(7.25 * SAMPLE_RATE)]


def test_tail_words():
    assert tail_words("one two three four", 2) == "three four"
    assert tail_words("one two", 0) == ""


# ------------------------------------------------------------------ in the pipeline
class BurstTranscriber:
    """One piece per tone burst in the clip, with clip-relative times."""

    def __init__(self) -> None:
        self.clips: list[float] = []
        self.prompts: list[str] = []

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[TranscriptPiece]:
        self.clips.append(audio.size / SAMPLE_RATE)
        self.prompts.append(prompt)
        hop = SAMPLE_RATE // 100
        loud = [float(np.abs(audio[i: i + hop]).max()) > 0.05
                for i in range(0, audio.size - hop + 1, hop)]
        out, start = [], None
        for i, on in enumerate(loud + [False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                seg = audio[start * hop: i * hop]
                out.append(TranscriptPiece(start / 100, i / 100,
                                           f"tone {dominant_freq(seg)} n{len(self.clips)}"))
                start = None
        return out


class RecordingDiarizer:
    voices: list = []

    def __init__(self) -> None:
        self.freqs: list[int] = []

    def diarize_runs(self, runs, pieces):
        for p in pieces:
            clip = runs[p.run][int(p.start * SAMPLE_RATE): int(p.end * SAMPLE_RATE)]
            self.freqs.append(dominant_freq(clip))
        return [SpeakerLabel("Unknown", False) for _ in pieces], {}


def _session(pipeline):
    # chunk 0: 200 Hz at 1-3 s, 600 Hz at 8-10 s, 1000 Hz at 20-22 s; chunk 1: 400 Hz at 32-34
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=0, started_at=T0,
                          codec="wav", data=wav(silence(1), tone(200, 2), silence(5),
                                                tone(600, 2), silence(10), tone(1000, 2),
                                                silence(8)))
    pipeline.ingest_chunk(device="omi", session_id="s1", sequence=1, started_at=T0 + 30,
                          codec="wav", data=wav(silence(2), tone(400, 2), silence(26)))
    pipeline.end_session("s1")


@pytest.mark.parametrize("window", [0.0, 8.0, 28.0])
def test_pipeline_maps_window_pieces_back_to_run_time(pipeline, window):
    pipeline.settings.whisper_window_seconds = window
    pipeline.settings.whisper_window_gap_seconds = 1.0
    pipeline.transcriber = fake = BurstTranscriber()
    pipeline.diarizer = diar = RecordingDiarizer()
    _session(pipeline)
    segs, _, _ = pipeline._transcribe_session("s1")
    assert [s.text.split()[1] for s in segs] == ["200", "600", "1000", "400"]
    for seg, true_start in zip(segs, [1, 8, 20, 32], strict=True):
        assert seg.started_at - T0 == pytest.approx(true_start, abs=0.05)
        assert seg.ended_at - T0 == pytest.approx(true_start + 2, abs=0.05)
    # the diarizer's slices of the run audio hold the same tone as the text says
    assert diar.freqs == [200, 600, 1000, 400]
    if window:
        assert max(fake.clips) <= window + 1e-6
        assert len(fake.clips) < 4  # regions were packed
    else:
        assert len(fake.clips) == 4


def test_prompt_carries_previous_clip_text(pipeline):
    s = pipeline.settings
    s.whisper_window_seconds, s.whisper_context_words, s.whisper_prompt = 8.0, 3, "Lisbon."
    pipeline.transcriber = fake = BurstTranscriber()
    pipeline.diarizer = RecordingDiarizer()
    _session(pipeline)
    pipeline._transcribe_session("s1")
    assert fake.prompts[0].startswith("Lisbon.")
    # vocabulary first, then the last 3 words of what came before
    assert fake.prompts[1] == "Lisbon. tone 600 n1"
    assert all(p.startswith("Lisbon.") for p in fake.prompts)


def test_no_carry_over_by_default(pipeline):
    pipeline.settings.whisper_window_seconds = 8.0
    pipeline.settings.whisper_prompt = "Lisbon."
    pipeline.transcriber = fake = BurstTranscriber()
    pipeline.diarizer = RecordingDiarizer()
    _session(pipeline)
    pipeline._transcribe_session("s1")
    assert set(fake.prompts) == {"Lisbon."}
