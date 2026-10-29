"""Fixed rules that discard obvious junk before the triage model call.

They only ever discard, and only when no word in the transcript could matter: every
word is filler ("okay", "mm", "yeah"), or the whole transcript is a microphone check
("testing, one, two"). Anything else returns ``None`` and goes to the model.

There is deliberately no rule that keeps a conversation without asking the model: the
triage call is what spots a TV or podcast playing, and a long transcript can be TV.
"""

from __future__ import annotations

import re


def _set(words: str) -> frozenset[str]:
    return frozenset(words.split())


# Words that carry no content on their own. "yes" and "no" are answers, not filler, so
# they are not here; neither is anything in another language (that goes to the model).
FILLER = _set("""
    ah aha ahh alright bye cool eh er erm good great ha haha hah hey hi hm hmm hmmm
    huh mhm mhmm mm mm-hmm mmhm mmm nice oh ok okay oops right so sorry sure thanks
    thank uh uh-huh uhm um umm well whoa wow yeah yep you yup
""")

# A microphone check uses only these words, and at least one of _MIC_WORDS.
_MIC_WORDS = frozenset({"test", "testing", "mic", "microphone"})
_MIC_CHECK = _MIC_WORDS | FILLER | _set("""
    check checking one two three four five 1 2 3 4 5 is this it on can you hear me
""")

# Words: letters and digits of any script, joined by an apostrophe or hyphen inside.
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")


def words(texts: list[str]) -> list[str]:
    return [w.replace("’", "'") for t in texts for w in _WORD.findall(t.lower())]


def discard_reason(texts: list[str]) -> str | None:
    """``"filler"`` or ``"mic_check"`` if the transcript is plainly junk, else ``None``."""
    ws = words(texts)
    if not ws:
        return None
    if all(w in FILLER for w in ws):
        return "filler"
    if any(w in _MIC_WORDS for w in ws) and all(w in _MIC_CHECK for w in ws):
        return "mic_check"
    return None
