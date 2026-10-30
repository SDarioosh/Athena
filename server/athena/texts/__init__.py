"""Text messages (iMessage first) as Athena conversations (docs/messages.md).

A message is a transcript line (``segments``, kind ``text``) plus its receipt
(``text_messages``); a thread of a chat is a conversation (``text_threads``).

* ``contract``: the upload's shape and its checks (``POST /v1/ingest/messages``).
* ``threads``: how a chat's messages are cut into threads, and which old thread each new
  one continues; pure functions.
* ``repo``: ``TextsRepo``, the ``Store`` methods: store an upload, thread again, delete a
  chat, the labels and the summary queue.
* ``summary``: what a thread's summary is told, and when it gets one.
"""
