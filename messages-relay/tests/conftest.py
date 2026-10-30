"""Fictional Messages and Contacts databases, typedstream blobs and a fake server.

Table and column names are the real ones (macOS 10.15 and 13+); all data is made up."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from athena_messages_relay.chatdb import APPLE_EPOCH

# 2026-10-01 12:00:00 UTC
T0 = 1790856000.0


def ts_blob(text: str) -> bytes:
    """An attributedBody like the ones Messages writes: an archived NSAttributedString."""
    raw = text.encode("utf-8")
    if len(raw) < 0x80:
        size = bytes([len(raw)])
    elif len(raw) < 0x10000:
        size = b"\x81" + len(raw).to_bytes(2, "little")
    else:
        size = b"\x82" + len(raw).to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
            b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + size + raw + b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00\x86")


def apple(ts: float, ns: bool = True) -> int:
    v = ts - APPLE_EPOCH
    return int(v * 1e9) if ns else int(v)


class ChatDBBuilder:
    def __init__(self, path: Path, schema: str = "ventura") -> None:
        self.path = path
        self.schema = schema
        self.con = sqlite3.connect(str(path), isolation_level=None)
        self.con.execute("PRAGMA journal_mode = WAL")
        extra = (", date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0,"
                 " thread_originator_guid TEXT") if schema == "ventura" else ""
        self.con.executescript(
            "CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL,"
            " service TEXT);"
            "CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE,"
            " chat_identifier TEXT, display_name TEXT, style INTEGER, service_name TEXT);"
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE,"
            " text TEXT, handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER,"
            " is_from_me INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,"
            " item_type INTEGER DEFAULT 0, attributedBody BLOB,"
            " cache_has_attachments INTEGER DEFAULT 0" + extra + ");"
            "CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER,"
            " message_date INTEGER DEFAULT 0, PRIMARY KEY (chat_id, message_id));"
            "CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);"
            "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,"
            " filename TEXT, mime_type TEXT, transfer_name TEXT);"
            "CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);")
        self.handles: Dict[str, int] = {}
        self.chats: Dict[str, int] = {}
        self.n = 0

    def handle(self, value: str) -> int:
        if value not in self.handles:
            cur = self.con.execute("INSERT INTO handle (id, service) VALUES (?, 'iMessage')",
                                   (value,))
            self.handles[value] = cur.lastrowid
        return self.handles[value]

    def chat(self, guid: str, handles: List[str], name: Optional[str] = None,
             group: bool = False) -> int:
        cur = self.con.execute(
            "INSERT INTO chat (guid, chat_identifier, display_name, style, service_name)"
            " VALUES (?, ?, ?, ?, 'iMessage')", (guid, guid.split(";")[-1], name or "",
                                                 43 if group else 45))
        self.chats[guid] = cur.lastrowid
        for h in handles:
            self.con.execute("INSERT INTO chat_handle_join VALUES (?, ?)",
                             (cur.lastrowid, self.handle(h)))
        return cur.lastrowid

    def message(self, chat: str, text: Optional[str] = None, *, body: Optional[bytes] = None,
                from_me: bool = False, handle: Optional[str] = None,
                at: Optional[float] = None, assoc: int = 0, item: int = 0,
                reply_to: Optional[str] = None, guid: Optional[str] = None,
                attachments: Tuple[Tuple[str, Optional[str]], ...] = (),
                ns: bool = True) -> Tuple[int, str]:
        self.n += 1
        guid = guid or "MSG-%04d" % self.n
        at = T0 + self.n * 60 if at is None else at
        cols = ["guid", "text", "handle_id", "date", "is_from_me", "associated_message_type",
                "item_type", "attributedBody", "cache_has_attachments"]
        vals: List[Any] = [guid, text, 0 if from_me or not handle else self.handle(handle),
                           apple(at, ns), int(from_me), assoc, item, body, int(bool(attachments))]
        if self.schema == "ventura":
            cols.append("thread_originator_guid")
            vals.append(reply_to)
        cur = self.con.execute("INSERT INTO message (%s) VALUES (%s)"
                               % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        rowid = cur.lastrowid
        self.con.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)",
                         (self.chats[chat], rowid, apple(at)))
        for name, mime in attachments:
            a = self.con.execute(
                "INSERT INTO attachment (guid, filename, mime_type, transfer_name)"
                " VALUES (?, ?, ?, ?)", ("AT-%d" % rowid, "~/Library/Messages/Attachments/"
                                         + name, mime, name))
            self.con.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
                             (rowid, a.lastrowid))
        return rowid, guid

    def edit(self, rowid: int, text: str, at: float) -> None:
        self.con.execute("UPDATE message SET text = ?, date_edited = ? WHERE ROWID = ?",
                         (text, apple(at), rowid))

    def unsend(self, rowid: int, at: float) -> None:
        self.con.execute("UPDATE message SET text = NULL, attributedBody = NULL,"
                         " date_retracted = ? WHERE ROWID = ?", (apple(at), rowid))


def make_contacts(root: Path, people: List[Tuple[str, str, List[str], List[str]]],
                  source: Optional[str] = None) -> Path:
    """``people``: (first, last, phones, emails). ``source``: under Sources/<source>/."""
    folder = root / "Sources" / source if source else root
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "AddressBook-v22.abcddb"
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT,"
        " ZNICKNAME TEXT, ZORGANIZATION TEXT);"
        "CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,"
        " ZFULLNUMBER TEXT);"
        "CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,"
        " ZADDRESS TEXT);")
    for i, (first, last, phones, emails) in enumerate(people, 1):
        con.execute("INSERT INTO ZABCDRECORD VALUES (?, ?, ?, NULL, NULL)", (i, first, last))
        for p in phones:
            con.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?, ?)",
                        (i, p))
        for e in emails:
            con.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?, ?)",
                        (i, e))
    con.commit()
    con.close()
    return path


class FakeServer:
    """Answers each request with the next of ``replies`` ((status, body)); then 200 ok."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.replies: List[Tuple[int, Dict[str, Any]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size) if size else b""
                server.requests.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "json": json.loads(body) if body else None})
                status, reply = (server.replies.pop(0) if server.replies
                                 else (200, {"ok": True}))
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = _answer
            do_DELETE = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/ingest/messages" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def posts(self) -> List[Dict[str, Any]]:
        return [r["json"] for r in self.requests if r["method"] == "POST"]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture
def chatdb(tmp_path):
    b = ChatDBBuilder(tmp_path / "chat.db")
    yield b
    b.con.close()
