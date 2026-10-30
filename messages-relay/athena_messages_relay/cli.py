"""``python3 -m athena_messages_relay <command>``: config, doctor, chats, exclude, include,
once, run, install, uninstall."""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import SOURCE, __version__
from . import config as config_mod
from . import state as state_mod
from .chatdb import ChatDB
from .contacts import Contacts
from .sync import Syncer, matches
from .upload import ServerUnavailable, Uploader, UploadError

log = logging.getLogger("athena_messages_relay")
LABEL = "org.athena.messages-relay"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
EDIT_CHECK_SECONDS = 300
MAX_BACKOFF = 600


def _python() -> str:
    return os.path.realpath(sys.executable)


def _fda_help(error: Exception, path: str) -> str:
    return (
        "Can't open %s (%s).\n"
        "Give Full Disk Access to the program that runs the relay:\n"
        "  System Preferences > Security & Privacy > Privacy > Full Disk Access\n"
        "  (macOS 13+: System Settings > Privacy & Security > Full Disk Access),\n"
        "  click the lock, then +, press Cmd-Shift-G and paste:\n"
        "    %s\n"
        "  When you run it in Terminal, Terminal needs Full Disk Access too."
        % (path, error, _python()))


def _open_db(cfg: config_mod.Config) -> ChatDB:
    return ChatDB(cfg.chat_db)


def _syncer(cfg: config_mod.Config, db: Optional[ChatDB] = None) -> Syncer:
    contacts = Contacts(cfg.contacts_dir)
    contacts.reload_if_changed()
    return Syncer(cfg, db or _open_db(cfg), contacts, Uploader(cfg.url, cfg.token),
                  state_mod.state_path(config_mod.config_path()))


def _need_config(cfg: config_mod.Config) -> Optional[str]:
    missing = cfg.missing()
    if missing:
        return ("not configured: set %s with `python3 -m athena_messages_relay config`"
                % " and ".join(missing))
    return None


# ---------------------------------------------------------------- commands
def cmd_config(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    if args.url:
        cfg.url = args.url
    if args.token:
        cfg.token = args.token
    if args.since is not None:
        cfg.since = args.since or None
    if args.no_groups:
        cfg.include_groups = False
    if args.groups:
        cfg.include_groups = True
    if not cfg.url:
        cfg.url = input("Athena ingest URL (http://<server>:8000/v1/ingest/messages): ").strip()
    if not cfg.token:
        cfg.token = getpass.getpass("Token: ").strip()
    if cfg.since:
        datetime.strptime(cfg.since, "%Y-%m-%d")  # fails loudly on a bad date
    path = config_mod.save(cfg)
    print("saved %s" % path)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    ok = True
    print("athena-messages-relay %s, Python %s (%s)" % (__version__, sys.version.split()[0],
                                                       _python()))
    problem = _need_config(cfg)
    print(("FAIL: " + problem) if problem else "ok: config %s" % config_mod.config_path())
    ok = ok and not problem
    try:
        db = _open_db(cfg)
        total = db.count()
        chats = db.chats()
    except sqlite3.Error as e:
        print("FAIL: " + _fda_help(e, cfg.chat_db))
        return 1
    print("ok: Messages database: %d messages in %d chats; edits and unsends %s"
          % (total, len(chats), "read" if db.has_edits else "not kept by this macOS"))
    recent = db.messages_after(max(0, db.max_rowid() - 1000), 1000)
    bad = sum(1 for m in recent if m.undecoded)
    if bad:
        print("warn: %d of the last %d messages have text that could not be decoded"
              % (bad, len(recent)))
    contacts = Contacts(cfg.contacts_dir)
    contacts.reload_if_changed()
    if contacts.names:
        print("ok: Contacts: %d phone numbers and emails with a name" % len(contacts.names))
    else:
        print("warn: no names from Contacts (empty, or no access): lines will show numbers")
    if not problem:
        try:
            Uploader(cfg.url, cfg.token, retries=0).post({"source": SOURCE, "messages": []})
            print("ok: server %s accepts uploads" % cfg.url)
        except (UploadError, ServerUnavailable) as e:
            print("FAIL: server %s: %s" % (cfg.url, e))
            ok = False
    return 0 if ok else 1


def cmd_chats(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    db = _open_db(cfg)
    contacts = Contacts(cfg.contacts_dir)
    contacts.reload_if_changed()
    syncer = Syncer(cfg, db, contacts, Uploader(cfg.url, cfg.token),
                    state_mod.state_path(config_mod.config_path()))
    stats = db.chat_stats()
    rows = sorted(syncer.chats.values(), key=lambda c: -(stats.get(c.rowid, (0, 0))[1] or 0))
    for chat in rows[:args.limit]:
        n, last = stats.get(chat.rowid, (0, None))
        when = datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else "-"
        flag = "excluded" if syncer.excluded(chat) else ""
        print("%-10s %6d  %-8s %s  [%s] %s" % (when, n, "group" if chat.group else "",
                                             syncer.chat_name(chat), chat.guid, flag))
    return 0


def cmd_exclude(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    entry = args.match.strip()
    if entry.lower() not in (e.lower() for e in cfg.exclude):
        cfg.exclude.append(entry)
        config_mod.save(cfg)
    db = _open_db(cfg)
    contacts = Contacts(cfg.contacts_dir)
    contacts.reload_if_changed()
    hit = [c for c in db.chats().values() if matches(c, entry, contacts)]
    print("excluded %r: %d chat(s). They are removed from the server on the next pass"
          " (within 5 minutes while the relay runs, or now with `once`)." % (entry, len(hit)))
    return 0


def cmd_include(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    entry = args.match.strip().lower()
    before = len(cfg.exclude)
    cfg.exclude = [e for e in cfg.exclude if e.lower() != entry]
    if len(cfg.exclude) == before:
        print("%r was not excluded" % args.match)
        return 1
    config_mod.save(cfg)
    db = _open_db(cfg)
    contacts = Contacts(cfg.contacts_dir)
    contacts.reload_if_changed()
    sfile = state_mod.state_path(config_mod.config_path())
    with state_mod.locked(sfile):
        syncer = Syncer(cfg, db, contacts, Uploader(cfg.url, cfg.token), sfile)
        again = [c for c in syncer.chats.values()
                 if matches(c, args.match, contacts) and not syncer.excluded(c)]
        for chat in again:
            syncer.include_again(chat)
    print("included %r again: %d chat(s) are sent from the start on the next pass"
          % (args.match, len(again)))
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    problem = _need_config(cfg)
    if problem:
        print(problem)
        return 1
    _log_to_stderr(args.verbose)
    try:
        syncer = _syncer(cfg)
    except sqlite3.Error as e:
        print(_fda_help(e, cfg.chat_db))
        return 1
    with state_mod.locked(syncer.state_file):
        try:
            counts = syncer.run_once()
        except (UploadError, ServerUnavailable) as e:
            print("upload failed: %s (nothing is lost: the next pass sends it again)" % e)
            return 1
    print(counts)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _log_to_file(args.verbose)
    log.info("relay %s starting (Python %s)", __version__, sys.version.split()[0])
    run_forever()
    return 0


def run_forever(max_loops: Optional[int] = None, sleep=time.sleep) -> None:
    """Poll the database's newest ROWID (cheap); sync when it moved, and every 5 minutes
    for edits, unsends, exclusions and Contacts changes. Back off on errors."""
    cfg_file = config_mod.config_path()
    cfg_stamp = None
    cfg = None
    syncer: Optional[Syncer] = None
    last_max = -1
    last_full = 0.0
    wait = 15.0
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            stamp = cfg_file.stat().st_mtime if cfg_file.exists() else None
            if syncer is None or stamp != cfg_stamp:
                cfg = config_mod.load(cfg_file)
                problem = _need_config(cfg)
                if problem:
                    raise UploadError(problem)
                if syncer is not None:
                    syncer.db.close()
                syncer = _syncer(cfg)
                cfg_stamp = stamp
                last_full = 0.0  # an exclusion may have changed: a full pass now
            assert cfg is not None and syncer is not None
            wait = cfg.poll_seconds
            now = time.time()
            current = syncer.db.max_rowid()
            names = syncer.contacts.reload_if_changed()
            if current != last_max or names or now - last_full >= EDIT_CHECK_SECONDS:
                with state_mod.locked(syncer.state_file):
                    counts = syncer.run_once()
                if counts.sent or counts.changed or counts.chats_removed:
                    log.info("%s", counts)
                last_max = current
                if now - last_full >= EDIT_CHECK_SECONDS:
                    last_full = now
        except UploadError as e:
            log.error("the server refused an upload: %s; trying again in %ds", e, MAX_BACKOFF)
            wait = MAX_BACKOFF
        except (ServerUnavailable, sqlite3.Error, OSError) as e:
            wait = min(max(wait, 15.0) * 2, MAX_BACKOFF)
            log.warning("%s: %s; trying again in %ds", type(e).__name__, e, wait)
            if isinstance(e, sqlite3.Error) and syncer is not None:
                syncer.db.close()
                syncer = None  # open the database again
        sleep(wait)


def cmd_install(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    problem = _need_config(cfg)
    if problem:
        print(problem)
        return 1
    package_dir = str(Path(__file__).resolve().parent.parent)
    config_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [_python(), "-m", "athena_messages_relay", "run"],
        "WorkingDirectory": package_dir,
        "EnvironmentVariables": {"PYTHONPATH": package_dir},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(config_mod.LOG_DIR / "launchd.out.log"),
        "StandardErrorPath": str(config_mod.LOG_DIR / "launchd.err.log"),
    }
    if PLIST.exists():
        subprocess.call(["launchctl", "unload", str(PLIST)])
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(str(PLIST), "wb") as f:
        plistlib.dump(plist, f)
    code = subprocess.call(["launchctl", "load", "-w", str(PLIST)])
    print("installed %s (launchctl load: %s)\nIt runs %s; that program needs Full Disk Access."
          % (PLIST, "ok" if code == 0 else "exit %d" % code, _python()))
    return code


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not PLIST.exists():
        print("not installed")
        return 0
    subprocess.call(["launchctl", "unload", "-w", str(PLIST)])
    PLIST.unlink()
    print("removed %s (config and state are kept in %s)" % (PLIST, config_mod.APP_DIR))
    return 0


# ---------------------------------------------------------------- logging
def _log_to_stderr(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(message)s")


def _log_to_file(verbose: bool) -> None:
    config_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(config_mod.LOG_DIR / "relay.log"), maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m athena_messages_relay",
                                     description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("config", help="set the server URL, token and options")
    p.add_argument("--url")
    p.add_argument("--token")
    p.add_argument("--since", help="first day to send, YYYY-MM-DD ('' = all)")
    p.add_argument("--no-groups", action="store_true", help="leave group chats out")
    p.add_argument("--groups", action="store_true", help="send group chats (the default)")
    p.set_defaults(func=cmd_config)
    sub.add_parser("doctor", help="check access, Contacts and the server").set_defaults(
        func=cmd_doctor)
    p = sub.add_parser("chats", help="list chats, newest first")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_chats)
    p = sub.add_parser("exclude", help="leave a chat out (and remove it from the server)")
    p.add_argument("match", help="chat id, group name, phone number, email or contact name")
    p.set_defaults(func=cmd_exclude)
    p = sub.add_parser("include", help="undo exclude: the chat is sent again from the start")
    p.add_argument("match")
    p.set_defaults(func=cmd_include)
    sub.add_parser("once", help="send what's new, then exit").set_defaults(func=cmd_once)
    sub.add_parser("run", help="keep sending (what the launchd agent runs)").set_defaults(
        func=cmd_run)
    sub.add_parser("install", help="start at login with launchd").set_defaults(
        func=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the launchd agent").set_defaults(
        func=cmd_uninstall)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)
