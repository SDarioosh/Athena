"""SQLite storage: the source of truth for sessions, transcripts and knowledge.

All timestamps are UTC epoch seconds (REAL). The API layer formats them as ISO-8601.

The code is in ``athena/db/`` (the connection, the migrations and one part of ``Store``
per area) and ``athena/domain.py`` (kinds, statuses, ``Segment``, title and name rules).
This module keeps the old import path: it re-exports ``Store`` and every name it had.
"""

from __future__ import annotations

from .db.action_items import (  # noqa: F401
    _valid_date,
    normalize_hhmm,
)
from .db.carry_over import (  # noqa: F401
    _carry_over,
    _chain_end,
    _insert_fact,
    _knowledge,
    _retire,
    _same_item,
)
from .db.connection import (  # noqa: F401
    BUSY_TIMEOUT_MS,
    LOCK_WAIT_WARN_SECONDS,
    _begin_immediate,
    _init_lock,
    _row,
)
from .db.conversations import (  # noqa: F401
    _delete_conversation,
    _move_knowledge,
    _move_offsets,
    _says_words,
)
from .db.facts import (  # noqa: F401
    QUOTE_CHARS,
)
from .db.forget import (  # noqa: F401
    _remember_erased_line,
    _take_down_digests,
)
from .db.matching import (  # noqa: F401
    _ALIKE,
    _CITE_SLACK,
    _SAME_LINE,
    _STOPWORDS,
    _STOPWORDS_TEXT,
    _cited_line,
    _line_key,
    _line_words,
    _norm,
    _same,
)
from .db.migrations import (  # noqa: F401
    LATER_SCHEMA,
    LINE_INDEX_SCHEMA,
    MEETINGS_SCHEMA,
    MIGRATIONS,
    PEOPLE_SCHEMA,
    SCHEMA,
    SCHEMA_VERSION,
    VOICE_SAMPLES_SCHEMA,
    _add_column,
    _has_table,
    _in_transaction,
    _m01_first_schema,
    _m02_line_index,
    _m03_voice_samples,
    _m04_focused_summaries,
    _m05_session_kind,
    _m06_calendar_items,
    _m07_list_name,
    _m08_note_title,
    _m09_retired_facts,
    _m10_people,
    _m11_item_titles,
    _m12_possessive_names,
    _m13_forgetting,
    _m14_fact_kind,
    _m15_chunk_tracks,
    _m16_meetings,
    _m17_unnumbered_changes,
    _run_script,
    _statements,
    _step,
    migrate,
)
from .db.people import (  # noqa: F401
    _add_alias,
    _ensure_person,
    _is_owner,
    _live_person,
    _lower,
    _person_id,
    _prune_candidates,
    _refresh_voiceprint,
    _rename_person,
    _touch_conversations,
    _unit,
    _voice_of,
)
from .db.search import (  # noqa: F401
    _fts_query,
)
from .db.sessions import (  # noqa: F401
    DuplicateChunk,
    MeetingClosed,
    _segment,
)
from .db.store import (  # noqa: F401
    Store,
)
from .db.voices import (  # noqa: F401
    MAX_LEARNED_SAMPLES,
)
from .domain import (  # noqa: F401
    _ACTIVE_IN,
    _LIST_NOISE,
    _NO_LIST,
    _NOTES_NOISE,
    _PLACEHOLDER_SPEAKER,
    _POSSESSIVE,
    CHAT,
    CONVERSATION,
    ERASED_TEXT,
    EXPLICIT_KINDS,
    IN_PERSON,
    ITEM_KINDS,
    ITEM_STATUSES,
    MEETING,
    MEETING_ACTIVE,
    MEETING_CLOSED_STATUSES,
    NOTE,
    NOTE_ITEM,
    SESSION_KINDS,
    SPEECH,
    TITLE_MAX,
    UNSET,
    VOICE,
    WRITTEN,
    WRITTEN_DEVICE,
    WRITTEN_SESSION_PREFIX,
    ZOOM_DEVICE,
    Segment,
    _device_name,
    clean_list_name,
    clean_notes_name,
    is_explicit,
    person_name,
    short_title,
)
