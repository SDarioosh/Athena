"""Names for handles, from the Mac's Contacts databases (read-only).

Contacts keeps one database per account under ``AddressBook/Sources/<id>/`` plus one at
the top. Phone numbers are matched on their last 10 digits (so "+1 (555) 010-0100" and
"5550100100" are the same), emails in lower case."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

log = logging.getLogger(__name__)
DB_NAME = "AddressBook-v22.abcddb"


def phone_key(value: str) -> Optional[str]:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 7:
        return None
    return digits[-10:]


def handle_key(handle: str) -> Optional[str]:
    """The key a handle (a phone number or an email) is looked up by."""
    h = handle.strip()
    if "@" in h:
        return h.lower()
    return phone_key(h)


def open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _full_name(row: sqlite3.Row) -> Optional[str]:
    parts = [row["ZFIRSTNAME"], row["ZLASTNAME"]]
    name = " ".join(p.strip() for p in parts if p and p.strip())
    for alt in (row["ZNICKNAME"], row["ZORGANIZATION"]):
        if not name and alt and alt.strip():
            name = alt.strip()
    return name or None


class Contacts:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser()
        self._mtimes: Optional[Tuple[Tuple[str, float], ...]] = None
        self.names: Dict[str, str] = {}

    def files(self) -> List[Path]:
        found = [self.root / DB_NAME] + sorted(self.root.glob("Sources/*/" + DB_NAME))
        return [p for p in found if p.exists()]

    def _stamp(self) -> Tuple[Tuple[str, float], ...]:
        out = []
        for p in self.files():
            for f in (p, p.with_name(p.name + "-wal")):
                with contextlib.suppress(OSError):
                    out.append((str(f), f.stat().st_mtime))
        return tuple(out)

    def reload_if_changed(self) -> bool:
        """Read the databases again when any of them changed; True when names changed."""
        stamp = self._stamp()
        if stamp == self._mtimes:
            return False
        self._mtimes = stamp
        names: Dict[str, str] = {}
        for path in self.files():
            try:
                self._read(str(path), names)
            except sqlite3.Error as e:  # no access, or not a Contacts database
                log.warning("contacts: can't read %s (%s)", path, e)
        changed = names != self.names
        self.names = names
        return changed

    @staticmethod
    def _read(path: str, names: Dict[str, str]) -> None:
        con = open_ro(path)
        try:
            people = {r["Z_PK"]: _full_name(r) for r in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD")}
            rows = [(r[0], r[1]) for r in con.execute(
                "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER")]
            rows += [(r[0], r[1]) for r in con.execute(
                "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS")]
        finally:
            con.close()
        for owner, value in rows:
            name = people.get(owner)
            key = handle_key(value) if value else None
            if name and key and key not in names:  # the first account's name wins
                names[key] = name

    def name_for(self, handle: Optional[str]) -> Optional[str]:
        if not handle:
            return None
        key = handle_key(handle)
        return self.names.get(key) if key else None
