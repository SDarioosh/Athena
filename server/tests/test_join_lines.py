"""Sentence fragments become one line (CONVO_JOIN_LINES)."""

from __future__ import annotations

from conftest import tone
from test_pipeline import ingest

from convo_server.diarize import UNSURE_LABEL, PieceRef, SpeakerLabel
from convo_server.lines import ends_sentence, join_lines

ME, BOB = SpeakerLabel("Me", True), SpeakerLabel("Bob", False)
UNSURE = SpeakerLabel(UNSURE_LABEL, False)


def _pieces(*lines: tuple[float, float, str], run: int = 0):
    return [(s, e, t, PieceRef(run, s, e)) for s, e, t in lines]


def test_one_speakers_fragments_become_one_line():
    pieces = _pieces((0.0, 0.6, "I just"), (0.8, 1.5, "found this"), (1.6, 2.0, "Nice."))
    out, labels = join_lines(pieces, [ME, ME, BOB], max_gap=1.0)
    assert [p[2] for p in out] == ["I just found this", "Nice."]
    assert out[0][:2] == (0.0, 1.5) and out[0][3] == PieceRef(0, 0.0, 1.5)
    assert labels == [ME, BOB]


def test_an_unknown_fragment_takes_the_name_of_the_sentence_it_continues():
    pieces = _pieces((0.0, 1.0, "You don't actually"), (1.1, 2.0, "use Claude."),
                     (2.1, 3.0, "Oh, don't I"), (3.1, 3.5, "know."))
    out, labels = join_lines(pieces, [ME, UNSURE, UNSURE, BOB], max_gap=1.0)
    assert [p[2] for p in out] == ["You don't actually use Claude.", "Oh, don't I know."]
    assert labels == [ME, BOB]


def test_lines_stay_apart():
    # a finished sentence, two different named speakers, a long pause, another run
    pieces = _pieces((0.0, 1.0, "Is that the firmware?"), (1.1, 2.0, "yes"))
    assert len(join_lines(pieces, [ME, ME], 1.0)[0]) == 2
    pieces = _pieces((0.0, 1.0, "Okay, I've heard of"), (1.1, 2.0, "that."))
    assert join_lines(pieces, [ME, BOB], 1.0)[1] == [ME, BOB]
    pieces = _pieces((0.0, 1.0, "I just"), (3.0, 4.0, "found this"))
    assert len(join_lines(pieces, [ME, ME], 1.0)[0]) == 2
    pieces = _pieces((0.0, 1.0, "I just")) + _pieces((1.2, 2.0, "found this"), run=1)
    assert len(join_lines(pieces, [ME, ME], 1.0)[0]) == 2
    pieces = _pieces((0.0, 29.0, "a long ramble and"), (29.5, 31.0, "more"))
    assert len(join_lines(pieces, [ME, ME], 1.0, max_seconds=30.0)[0]) == 2


def test_without_a_diarizer_fragments_join_too():
    out, labels = join_lines(_pieces((0.0, 1.0, "I just"), (1.2, 2.0, "found this")),
                             None, 1.0)
    assert [p[2] for p in out] == ["I just found this"] and labels is None


def test_sentence_ends():
    assert ends_sentence("Done.") and ends_sentence('He said "stop!"')
    assert ends_sentence("Really?") and ends_sentence("so interestingly…")
    assert not ends_sentence("I just") and not ends_sentence("Oh, don't I")


class _TwoPieces:
    def transcribe(self, audio, prompt=""):
        from convo_server.transcribe import TranscriptPiece

        return [TranscriptPiece(0.2, 1.0, "I just"), TranscriptPiece(1.2, 2.5, "found this.")]


def _run(pipeline) -> list[str]:
    pipeline.transcriber = _TwoPieces()
    ingest(pipeline, 0, tone(200, 3))
    pipeline.end_session("s1")
    pipeline.process_session("s1")
    return [s.text for s in pipeline.store.session_segments("s1")]


def test_the_pipeline_joins_fragments(pipeline):
    assert _run(pipeline) == ["I just found this."]


def test_joining_can_be_turned_off(pipeline):
    pipeline.settings.join_lines = False
    assert _run(pipeline) == ["I just", "found this."]
