"""Join transcript lines that are one sentence cut in two (``CONVO_JOIN_LINES``).

Whisper ends segments mid-sentence, and the pyannote line split cuts at every turn
change it sees, so one person's "I just" / "found this" can become two lines, or two
lines with different labels. Two neighbouring lines are joined when the first doesn't
end a sentence, the pause between them is short, and the result is not too long:

* same speaker: always;
* one of them ``Unknown`` (the line check wasn't sure): the line takes the other's name,
  because the sentence going on is good evidence it is the same person;
* two different named speakers: never (a real interruption is common; the voices
  decided).
"""

from __future__ import annotations

import re
from typing import TypeVar

from .diarize import UNSURE_LABEL, PieceRef

L = TypeVar("L")

# A line ends a sentence when its last character (closing quotes and brackets aside) is
# one of these.
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*\s*$")


def ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip())) or not text.strip()


def join_lines(pieces: list[tuple[float, float, str, PieceRef]], labels: list[L] | None,
               max_gap: float, max_seconds: float = 30.0
               ) -> tuple[list[tuple[float, float, str, PieceRef]], list[L] | None]:
    """Join sentence fragments; ``pieces`` are (start, end, text, ref) in time order and
    ``labels`` (or None: no diarizer) their speakers (``SpeakerLabel``)."""
    if not pieces:
        return pieces, labels
    out = [pieces[0]]
    out_labels = [labels[0]] if labels is not None else None
    for i in range(1, len(pieces)):
        s, e, text, ref = pieces[i]
        ps, pe, ptext, pref = out[-1]
        lab = labels[i] if labels is not None else None
        merged = None
        if (ref.run == pref.run and not ends_sentence(ptext) and s - pe <= max_gap
                and max(e, pe) - ps <= max_seconds):
            merged = (None,) if out_labels is None else _joined_label(out_labels[-1], lab)
        if merged is None:
            out.append(pieces[i])
            if out_labels is not None:
                out_labels.append(lab)
            continue
        out[-1] = (ps, max(pe, e), f"{ptext.rstrip()} {text.lstrip()}",
                   PieceRef(pref.run, pref.start, max(pref.end, ref.end)))
        if out_labels is not None:
            out_labels[-1] = merged[0]
    return out, out_labels


def _joined_label(a, b) -> tuple | None:
    """The label of two joined lines, in a 1-tuple, or None: don't join."""
    if a == b:
        return (a,)
    if getattr(a, "name", None) == UNSURE_LABEL:
        return (b,)
    if getattr(b, "name", None) == UNSURE_LABEL:
        return (a,)
    return None
