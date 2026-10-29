"""Moved to ``athena.meetings.ingest.chat``; this old path is that same module object."""

import sys

from .meetings.ingest import chat as _m

sys.modules[__name__] = _m
