"""Moved to ``athena.meetings.transcribe.tracks``; this old path is that same module object."""

import sys

from .meetings.transcribe import tracks as _m

sys.modules[__name__] = _m
