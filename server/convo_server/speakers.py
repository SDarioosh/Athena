"""Speaker naming across conversations: relabel matches, suggestions for unnamed voices.

When someone is named (``name_speaker``) or enrolled, their voice is compared with the
stored voices of unnamed speakers ("Speaker N") in every other recording. Confident
matches are renamed and those conversations are marked for re-summary. "Confident" is
stricter than live matching: the score must reach ``CONVO_SPEAKER_RELABEL_THRESHOLD``
and beat every other known voice by ``RELABEL_MARGIN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diarize import EnrolledVoice, load_voices

RELABEL_MARGIN = 0.05


@dataclass(frozen=True)
class Relabel:
    session_id: str
    label: str
    name: str
    score: float
    lines: int
    conversations: list[str]


def best_matches(voices: list[EnrolledVoice], embedding: Any) -> list[tuple[float, str]]:
    """(score, name) for every known voice, best first."""
    return sorted(((v.score(embedding), v.name) for v in voices), reverse=True)


def relabel_matches(store: Any, name: str, threshold: float,
                    margin: float = RELABEL_MARGIN) -> list[Relabel]:
    """Rename unnamed speakers whose stored voice confidently matches ``name``."""
    voices = load_voices(store)
    if not any(v.name == name for v in voices):
        return []
    done: list[Relabel] = []
    for cand in store.unnamed_candidates():
        ranked = best_matches(voices, cand["embedding"])
        score, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        if best != name or score < threshold or score - runner_up < margin:
            continue
        n, convs = store.rename_session_label(cand["session_id"], cand["label"], name)
        if n:
            done.append(Relabel(cand["session_id"], cand["label"], name, round(score, 3), n,
                                convs))
    return done


def suggestion(voices: list[EnrolledVoice], embedding: Any, threshold: float
               ) -> tuple[str, float] | None:
    """The known voice an unnamed speaker sounds most like, if it is reasonably close."""
    ranked = best_matches(voices, embedding)
    if not ranked or ranked[0][0] < threshold:
        return None
    return ranked[0][1], ranked[0][0]
