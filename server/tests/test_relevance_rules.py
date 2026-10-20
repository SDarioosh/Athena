"""Fixed rules that discard plain filler and mic checks before the triage call."""

import pytest

from convo_server.relevance_rules import discard_reason


@pytest.mark.parametrize("texts", [
    ["Okay.", "Mm-hmm. Yeah, yeah.", "Uh-huh, sure. Thanks, bye."],
    ["Hmm.", "OKAY", "yeah… right, right"],
    ["Thank you.", "Thank you."],  # a common whisper line on near-silence
])
def test_filler_only_is_discarded(texts):
    assert discard_reason(texts) == "filler"


@pytest.mark.parametrize("texts", [
    ["Testing, testing, one, two, three."],
    ["Mic check.", "Is this on? Can you hear me?", "Okay, test 1 2 3."],
])
def test_mic_check_is_discarded(texts):
    assert discard_reason(texts) == "mic_check"


@pytest.mark.parametrize("texts", [
    [],
    [""],
    ["Okay, yeah, call mom."],                  # one content word is enough
    ["Yes.", "No.", "Yes."],                    # answers, not filler: the model decides
    ["Okay, sure, see you at five."],
    ["Hi Sarah.", "Okay."],                     # a name
    ["Testing the new oven tonight, okay?"],    # 'testing' in a real sentence
    ["Can you hear me?", "Yeah."],              # no mic word: may be a call
    ["Ja, ja, okay."],                          # another language: the model decides
    ["好的", "嗯"],
    ["Okay, 3 o'clock."],
])
def test_anything_else_goes_to_the_model(texts):
    assert discard_reason(texts) is None
