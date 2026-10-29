"""SQLite storage, by area. ``Store`` (``db/store.py``) is the one object callers use;
it opens the connection (``connection.py``), runs ``migrations.py`` and takes its methods
from one mixin per area (sessions, meetings, conversations, facts, forget, search, people,
voices, action items). ``athena.store`` re-exports all of it under the old path."""
