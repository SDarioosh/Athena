"""Moved to ``athena.meetings.people.names``; this old path is that same module object."""

import sys

from .meetings.people import names as _m

sys.modules[__name__] = _m
