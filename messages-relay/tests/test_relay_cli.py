from __future__ import annotations

import plistlib

import pytest

from athena_messages_relay import cli
from athena_messages_relay import config as config_mod
from athena_messages_relay.upload import Uploader

from conftest import make_contacts

MIA = "+15550100100"
CHAT = "iMessage;-;" + MIA


@pytest.fixture
def configured(tmp_path, monkeypatch, chatdb, server):
    monkeypatch.setenv("ATHENA_MESSAGES_CONFIG", str(tmp_path / "app" / "config.json"))
    monkeypatch.setattr(config_mod, "LOG_DIR", tmp_path / "logs")
    make_contacts(tmp_path / "ab", [("Mia", "Chen", [MIA], [])])
    config_mod.save(config_mod.Config(url=server.url, token="tok-0123456789abcdef",
                                      chat_db=str(chatdb.path),
                                      contacts_dir=str(tmp_path / "ab")))
    chatdb.chat(CHAT, [MIA])
    return tmp_path


def test_config_file_is_private(configured):
    path = config_mod.config_path()
    assert path.stat().st_mode & 0o077 == 0
    assert config_mod.load().token == "tok-0123456789abcdef"


def test_doctor(configured, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ok: Messages database: 0 messages in 1 chats" in out
    assert "ok: Contacts: 1" in out and "accepts uploads" in out


def test_doctor_explains_full_disk_access(configured, capsys, tmp_path):
    cfg = config_mod.load()
    cfg.chat_db = str(tmp_path / "nope" / "chat.db")
    config_mod.save(cfg)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "Full Disk Access" in out and cli._python() in out


def test_once_exclude_include_chats(configured, chatdb, server, capsys):
    chatdb.message(CHAT, "hi", handle=MIA)
    assert cli.main(["once"]) == 0
    assert "sent 1" in capsys.readouterr().out
    assert cli.main(["chats"]) == 0
    assert "Mia Chen" in capsys.readouterr().out
    assert cli.main(["exclude", "Mia Chen"]) == 0
    assert cli.main(["once"]) == 0
    assert [r["method"] for r in server.requests] == ["POST", "DELETE"]
    assert cli.main(["include", "mia chen"]) == 0
    assert cli.main(["once"]) == 0
    assert server.posts()[-1]["messages"][0]["text"] == "hi"


def test_run_loop_syncs_and_backs_off(configured, chatdb, server, monkeypatch):
    monkeypatch.setattr(cli, "Uploader", lambda url, token, **kw: Uploader(
        url, token, sleep=lambda s: None, **kw))
    chatdb.message(CHAT, "hi", handle=MIA)
    waits = []
    cli.run_forever(max_loops=2, sleep=waits.append)
    assert len(server.posts()) == 1 and waits == [15.0, 15.0]
    server.httpd.shutdown()
    server.httpd.server_close()
    chatdb.message(CHAT, "again", handle=MIA)
    waits.clear()
    cli.run_forever(max_loops=1, sleep=waits.append)
    assert waits and waits[0] == 30.0


def test_install_writes_the_agent(configured, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "PLIST", tmp_path / "LaunchAgents" / "agent.plist")
    monkeypatch.setattr(cli.subprocess, "call", lambda a: calls.append(a) or 0)
    assert cli.main(["install"]) == 0
    with open(str(cli.PLIST), "rb") as f:
        plist = plistlib.load(f)
    assert plist["ProgramArguments"][1:] == ["-m", "athena_messages_relay", "run"]
    assert plist["KeepAlive"] and plist["RunAtLoad"]
    assert calls[-1][:3] == ["launchctl", "load", "-w"]
    assert cli.main(["uninstall"]) == 0 and not cli.PLIST.exists()
