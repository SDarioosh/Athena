"""Online meetings: the bot launcher, ingest, transcription and people of a meeting.

- ``bot/``: the meeting bot launcher (``bot.launcher``, with the meeting link parsing)
  and Athena's Zoom sign-in (``bot.zoom_signin``).
- ``ingest/``: a meeting's chat, topic and participants (``ingest.chat``).
- ``transcribe/``: per-track transcription and bleed (``transcribe.tracks``) and live
  transcription while a meeting records (``transcribe.live``).
- ``people/``: display names (``people.names``) and voices learned from meetings
  (``people.voices``).

Importing this package loads nothing else: a server that never gets a meeting request
never imports the launcher (``athena.meetings.bot.launcher``), and importing ``people``
or ``transcribe`` doesn't either.
"""
