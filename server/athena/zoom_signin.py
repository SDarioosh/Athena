"""Moved to ``athena.meetings.bot.zoom_signin``; this old path is that same module object."""

import sys

from .meetings.bot import zoom_signin as _m

sys.modules[__name__] = _m
