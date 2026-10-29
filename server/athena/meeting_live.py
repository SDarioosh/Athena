"""Moved to ``athena.meetings.transcribe.live``; this old path is that same module object."""

import sys

from .meetings.transcribe import live as _m

sys.modules[__name__] = _m
