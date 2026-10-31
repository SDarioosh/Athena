from __future__ import annotations

import os

from athena_messages_relay.contacts import Contacts, handle_key

from conftest import make_contacts


def test_names_from_every_account(tmp_path):
    make_contacts(tmp_path, [("Mia", "Chen", ["+1 (555) 010-0100"], ["Mia@Example.com"])])
    make_contacts(tmp_path, [("Leo", "", ["555-010-0101"], [])], source="ABC-123")
    c = Contacts(str(tmp_path))
    assert c.reload_if_changed()
    assert c.name_for("+15550100100") == "Mia Chen"
    assert c.name_for("5550100100") == "Mia Chen"
    assert c.name_for("mia@example.com") == "Mia Chen"
    assert c.name_for("+1 555 010 0101") == "Leo"
    assert c.name_for("+15550199999") is None
    assert c.name_for(None) is None


def test_reload_only_when_a_file_changed(tmp_path):
    path = make_contacts(tmp_path, [("Mia", "Chen", ["+15550100100"], [])])
    c = Contacts(str(tmp_path))
    assert c.reload_if_changed()
    assert not c.reload_if_changed()
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("UPDATE ZABCDRECORD SET ZLASTNAME = 'Chen-Park'")
    con.commit()
    con.close()
    st = path.stat()
    os.utime(str(path), (st.st_atime, st.st_mtime + 5))
    assert c.reload_if_changed()
    assert c.name_for("+15550100100") == "Mia Chen-Park"


def test_no_contacts_is_fine(tmp_path):
    c = Contacts(str(tmp_path / "missing"))
    assert not c.reload_if_changed()
    assert c.name_for("+15550100100") is None


def test_handle_keys():
    assert handle_key("+1 (555) 010-0100") == "5550100100"
    assert handle_key(" Mia@Example.COM ") == "mia@example.com"
    assert handle_key("12345") is None
