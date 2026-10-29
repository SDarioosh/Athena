"""Moved to ``athena.meetings.people.voices``; this old path is that same module object."""

import sys

from .meetings.people import voices as _m

sys.modules[__name__] = _m
