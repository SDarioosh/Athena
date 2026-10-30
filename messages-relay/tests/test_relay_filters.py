from __future__ import annotations

import pytest

from athena_messages_relay import filters

CODES = [
    "Your verification code is 482913",
    "G-482913 is your Google verification code.",
    "123456 is your login code. Don't share it with anyone.",
    "Your Example Bank security code: 7731. Do not share this code.",
    "Use one-time passcode 55218 to sign in",
    "Your OTP is 9081",
    "Your code is 381-442",
    "Sign-in code: 448812",
]
PERSONAL = [
    "The door code is 4321, come on up",
    "Call me at 4pm",
    "We're at 1234 Main St",
    "My pin is in the drawer",
    "Can you send me the code for the wifi?",
    "Flight AB1234 lands at 18:05",
    "Dinner at 7?",
    "",
]


@pytest.mark.parametrize("text", CODES)
def test_codes_never_leave_the_mac(text):
    assert filters.is_one_time_code(text)
    assert not filters.keep(text, "+15550100", False)


@pytest.mark.parametrize("text", PERSONAL)
def test_personal_messages_are_sent(text):
    assert not filters.is_one_time_code(text)
    assert filters.keep(text, "+15550100", False)


def test_short_code_senders_are_left_out():
    assert not filters.keep("Your parcel arrives today", "55501", False)
    assert not filters.keep("Reply STOP to end", "555012", False)
    assert filters.keep("hi", "+15550100", False)
    assert filters.keep("hi", "mia@example.com", False)
    assert filters.keep("thanks!", None, True)
