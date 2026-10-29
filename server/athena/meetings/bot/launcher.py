"""Meeting bot launcher: ``POST``/``GET``/``DELETE /v1/meetings``.

Imported lazily by the API route handlers, on the first meeting request: a server that
never gets one never imports this module. Nothing here runs between meetings:

- The bot (``CONVO_MEETING_BOT_PATH``, ``athena-meeting-bot`` in ``meeting-bot/``) is a child
  process started on request, under safety caps (``systemd-run --user --scope`` with
  ``CPUQuota``, ``MemoryMax`` and ``RuntimeMaxSec`` when that works, else ``nice 10`` and
  ``RLIMIT_AS``).
- One asyncio task per running bot, on the server's own event loop, reads the bot's
  status lines from its stdout and waits for it to exit (a pidfd on the loop, so no
  thread). The task ends when the bot does; no thread, timer or poller is left.
- After a server restart, rows still marked active are checked on the next meeting
  request (is the pid still that bot?); there is no startup scan.

Bot contract (the bot codes against it; also in ``docs/api/meetings.md``)::

    <bot> --meeting-id <digits> --session-id <id> --upload-url <http://127.0.0.1:port>
          --name Athena --max-seconds <s> --alone-seconds <s> --waiting-room-seconds <s>
          --codec opus|wav --work-dir <dir>
    env:  ATHENA_MEETING_JWT       Meeting SDK join token (signed here, HS256)
          ATHENA_UPLOAD_TOKEN      bearer token for this meeting's session only
          ATHENA_MEETING_PASSCODE  passcode or the link's pwd value (absent if none)
          ATHENA_ZOOM_JOIN_TOKEN   ZAK or OBF token from Athena's Zoom sign-in (absent
                                   in mode none), see join_token_provider
          ATHENA_ZOOM_JOIN_TOKEN_KIND  zak | obf (absent with no join token)
    stdout: one status line per change: ``STATE <state> [<reason>]``; with an OBF token,
            ``STATE starting waiting_for_owner`` while Zoom refuses the join until the
            owner is in the meeting, ``STATE failed owner_not_joined`` after the wait,
            ``STATE ended owner_left`` when Zoom drops the bot as the owner leaves;
            ``STATE waiting_permission [denied]`` after asking the host once for
            recording permission (it waits; never asks again, never leaves over it;
            after a rejoin it asks once more only if the host had allowed it);
            ``STATE starting reconnecting`` after a network drop while the SDK
            reconnects or the bot rejoins (3 tries within 2 minutes, same session and
            per-track sequences), then ``recording`` / ``waiting_permission`` again or
            ``STATE ended connection_lost``; ``STATE ended waiting_room_timeout`` when
            the host moved the bot back to the waiting room after it was in (``failed``
            only if it never got in)
    stderr: free-form logs (go to the server's log)

Secrets never go on the command line (``ps`` shows it to every user), and the bot never
gets ``CONVO_INGEST_TOKEN``: its environment is built from scratch.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...config import Settings
from ...db.store import Store
from ...domain import MEETING_ACTIVE

log = logging.getLogger("athena.meetings")  # its name before the move

STATES = ("starting", "waiting_room", "waiting_permission", "recording", "ended", "failed")
TERMINAL = ("ended", "failed")
DEVICE = "zoom"  # the device name the bot uploads with
BOT_NAME = "Athena"
STOP_GRACE_SECONDS = 10.0  # after SIGTERM (the bot must exit within 5 s), then SIGKILL
LIFETIME_GRACE_SECONDS = 60.0  # beyond --max-seconds before the bot is killed
# systemd's RuntimeMaxSec comes this long after the launcher's own lifetime timeout, so
# the launcher (SIGTERM, then SIGKILL after STOP_GRACE_SECONDS) always stops it first
SYSTEMD_MARGIN_SECONDS = STOP_GRACE_SECONDS + 20.0
READER_DRAIN_SECONDS = 2.0  # after the bot exited, for its last status lines
MAX_LOG_LINE = 4096  # a longer stdout line is dropped whole, never parsed
MAX_TITLE_CHARS = 200
MAX_PASSCODE_CHARS = 128
JOIN_TOKEN_SECONDS = 7200  # a ZAK or OBF token lasts about 2 h (zoom_signin.py)
JOIN_TOKEN_MARGIN_SECONDS = 300  # the wait ends this long before the token does
# The bot reads its --*-seconds as whole numbers of at most 9 digits, at least 1
# (meeting-bot/bot/src/options.cpp); anything else and it exits "failed bad_arguments".
MAX_BOT_SECONDS = 999_999_999
# Server shutdown as a container's init (see Launcher.shutdown): the bots' SIGTERM wait,
# within Docker's default 10 s stop timeout (the bot exits within 5 s of SIGTERM).
SHUTDOWN_STOP_SECONDS = 6.0

_STATE_LINE = re.compile(r"^STATE (starting|waiting_room|waiting_permission|recording|ended|"
                         r"failed)"
                         r"(?: ([a-z0-9_]{1,40}))?$")
_MEETING_NUMBER = re.compile(r"^\d{9,11}$")
# a link pasted without a scheme: zoom.us, or zoomgov.com for its own refusal
_NO_SCHEME = re.compile(r"^([a-z0-9-]+\.)*zoom(?:\.us|gov\.com)/", re.IGNORECASE)
_ZOOM_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*zoom\.us$")
_PASSCODE = re.compile(r"^[!-~]{1,128}$")  # printable ASCII, no spaces
_CPU = re.compile(r"^\d{1,5}%$")
_MEMORY = re.compile(r"^(\d+)([KMGT]?)$", re.IGNORECASE)
# Environment the bot keeps from the server's (nothing else: no CONVO_* secrets).
_KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR",
             "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


class MeetingError(Exception):
    """A request the launcher refuses; ``status`` is the HTTP status."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status, self.detail, self.extra = status, detail, extra


# ------------------------------------------------------------------ Zoom links
@dataclass(frozen=True)
class ZoomMeeting:
    meeting_id: str
    passcode: str | None = None


def parse_zoom_link(text: str) -> ZoomMeeting:
    """The meeting number and passcode of a Zoom join link; ValueError with a message the
    owner can act on otherwise. The hub's meeting box mirrors these rules:

    - ``https://zoom.us/j/<number>?pwd=<pwd>`` and any subdomain of zoom.us
      (``us06web.zoom.us``, a company's vanity ``acme.zoom.us``); ``http`` and a link
      pasted without a scheme are read too. Also the web client's ``/wc/join/<number>``
      and ``/wc/<number>/join``. Path words (``j``, ``wc``, ``join``) in any case.
    - ``zoommtg://zoom.us/join?confno=<number>&pwd=<pwd>`` (and ``zoomus://``).
    - The number has 9 to 11 digits. ``pwd`` is optional and passed on unchanged.
    - Refused, each with its own message: personal links (``/my/<name>``: they carry no
      meeting number), webinars (``/w/``: not supported yet), registration links, host
      start links (``/s/``), Zoom for Government (``zoomgov.com``, with or without a
      scheme), other hosts and anything else.
    """
    raw = (text or "").strip().strip("<>\"'").strip()
    if not raw:
        raise ValueError("paste a Zoom meeting link")
    if "://" not in raw and _NO_SCHEME.match(raw):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as e:
        raise ValueError("not a Zoom meeting link") from e
    scheme = parts.scheme.lower()
    if host == "zoomgov.com" or host.endswith(".zoomgov.com"):
        raise ValueError("Zoom for Government links (zoomgov.com) aren't supported")
    if scheme not in ("https", "http", "zoommtg", "zoomus") or not _ZOOM_HOST.match(host) \
            or port is not None or parts.username or parts.password:
        raise ValueError("not a Zoom meeting link (expected https://…zoom.us/j/<number>)")
    query = parse_qs(parts.query)
    pwd = (query.get("pwd") or [None])[0] or None
    # path words (j, wc, join, …) in any case; the number is digits anyway
    segs = [s.lower() for s in parts.path.split("/") if s]
    if scheme in ("zoommtg", "zoomus"):
        if segs[:1] == ["start"]:
            raise ValueError("this is the host's start link; paste the join link")
        number = (query.get("confno") or [""])[0]
        if segs[:1] != ["join"] or not number:
            raise ValueError("not a Zoom meeting link (zoommtg:// links need confno)")
    else:
        first = segs[0] if segs else ""
        if first == "my":
            raise ValueError("personal links (/my/<name>) carry no meeting number; paste "
                             "the invite link with /j/<number>, or send meeting_id")
        if first == "w":
            raise ValueError("webinars are not supported yet")
        if first == "s":
            raise ValueError("this is the host's start link; paste the join link")
        if first in ("meeting", "webinar") and "register" in segs:
            raise ValueError("this is a registration link; register, then paste the join "
                             "link from the confirmation")
        if first == "j" and len(segs) == 2:
            number = segs[1]
        elif first == "wc" and len(segs) == 3 and segs[1] == "join":
            number = segs[2]
        elif first == "wc" and len(segs) == 3 and segs[2] == "join":
            number = segs[1]
        else:
            raise ValueError("not a Zoom meeting link (expected https://…zoom.us/j/<number>)")
    return ZoomMeeting(_meeting_number(number), _passcode(pwd))


def _meeting_number(value: str | int) -> str:
    number = re.sub(r"[\s-]", "", str(value))
    if not _MEETING_NUMBER.match(number):
        raise ValueError("a Zoom meeting number has 9 to 11 digits")
    return number


def _passcode(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _PASSCODE.match(value):
        raise ValueError(f"passcode must be at most {MAX_PASSCODE_CHARS} printable "
                         "characters without spaces")
    return value


def parse_join_request(url: str | None, meeting_id: str | int | None,
                       passcode: str | None) -> ZoomMeeting:
    """``{url}`` or ``{meeting_id, passcode}``, one of them; ValueError otherwise."""
    if url and meeting_id not in (None, ""):
        raise ValueError("send url or meeting_id, not both")
    if url:
        found = parse_zoom_link(url)
        if passcode:  # a passcode sent beside a link without pwd
            return ZoomMeeting(found.meeting_id, found.passcode or _passcode(passcode))
        return found
    if meeting_id in (None, ""):
        raise ValueError("send url (a Zoom meeting link) or meeting_id")
    return ZoomMeeting(_meeting_number(meeting_id), _passcode(passcode))


# ------------------------------------------------------------ join token (JWT)
def sign_sdk_jwt(key: str, secret: str, meeting_id: str, *, now: float | None = None,
                 lifetime: float = 7200.0) -> str:
    """The Meeting SDK join signature, signed here (HS256) from the owner's SDK key and
    secret; nothing is sent to Zoom to make it. Payload per Zoom's Meeting SDK auth docs
    (developers.zoom.us/docs/meeting-sdk/auth/, read 2026-10-27): ``appKey``, ``mn``,
    ``role`` 0 (participant), ``iat``, ``exp`` and ``tokenExp``, which must be 30 min to
    48 h after ``iat``. ``iat`` is 30 s in the past for clock skew."""
    iat = int(now if now is not None else time.time()) - 30
    life = int(min(max(lifetime, 1800), 48 * 3600))
    payload = {"appKey": key, "mn": int(meeting_id), "role": 0, "iat": iat,
               "exp": iat + life, "tokenExp": iat + life}

    def b64(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    head = b64({"alg": "HS256", "typ": "JWT"}) + "." + b64(payload)
    sig = hmac.new(secret.encode(), head.encode(), hashlib.sha256).digest()
    return head + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


async def join_token_provider(settings: Settings, meeting: ZoomMeeting, *,
                              transport: Any = None) -> tuple[str, str | None]:
    """``(kind, token)`` for this join: ``("zak"|"obf", token)`` from Athena's Zoom
    sign-in (athena/meetings/bot/zoom_signin.py, imported only here), or ``("none", None)``.
    Called on a join request only. Raises MeetingError 503 ``zoom_signin_needed`` when the
    mode needs a sign-in that is missing or expired, 502 ``zoom_unavailable`` when Zoom
    can't be reached."""
    if settings.zoom_join_token == "none":
        return "none", None
    from . import zoom_signin

    kind = zoom_signin.join_mode(settings)
    if kind == "none":
        return "none", None
    try:
        token = await zoom_signin.join_token(settings, kind, meeting.meeting_id,
                                             transport=transport)
    except zoom_signin.SigninNeeded as e:
        raise MeetingError(503, "zoom_signin_needed", message=str(e)) from None
    except zoom_signin.ZoomUnavailable as e:
        raise MeetingError(502, "zoom_unavailable", message=str(e)) from None
    return kind, token


# --------------------------------------------------------------- bot command
def memory_bytes(value: str) -> int:
    m = _MEMORY.match(value.strip())
    if not m:
        raise ValueError(f"CONVO_MEETING_BOT_MEMORY must look like 2G or 1536M, not {value!r}")
    return int(m.group(1)) * 1024 ** "_KMGT".index((m.group(2) or "_").upper())


def bot_args(settings: Settings, meeting: ZoomMeeting, session_id: str,
             work_dir: Path, *, join_token: bool = False) -> list[str]:
    """The bot's arguments. With a join token (valid about 2 h from now), the wait for
    the waiting room or the owner is capped below the token's lifetime, so the bot gives
    up with a clear reason instead of trying a dead token."""
    wait = settings.meeting_waiting_room_seconds
    if join_token:
        wait = min(wait, JOIN_TOKEN_SECONDS - JOIN_TOKEN_MARGIN_SECONDS)
    host = settings.host if settings.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return ["--meeting-id", meeting.meeting_id, "--session-id", session_id,
            "--upload-url", f"http://{host}:{settings.port}", "--name", BOT_NAME,
            "--max-seconds", str(int(settings.max_session_seconds)),
            "--alone-seconds", str(int(settings.meeting_alone_seconds)),
            "--waiting-room-seconds", str(int(wait)),
            "--codec", settings.meeting_track_codec, "--work-dir", str(work_dir)]


def build_command(settings: Settings, runner: str, bot: Path, args: list[str],
                  unit: str) -> list[str]:
    """The argv that starts the bot. No secret is ever in it."""
    if runner == "systemd":
        lifetime = math.ceil(settings.max_session_seconds + LIFETIME_GRACE_SECONDS
                             + SYSTEMD_MARGIN_SECONDS)
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--unit={unit}", "-p", f"CPUQuota={settings.meeting_bot_cpu}",
                "-p", f"MemoryMax={settings.meeting_bot_memory}",
                "-p", f"RuntimeMaxSec={lifetime}", "nice", "-n", "10", str(bot), *args]
    nice = shutil.which("nice")
    return [nice, "-n", "10", str(bot), *args] if nice else [str(bot), *args]


def bot_env(secrets_env: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    env.update({k: v for k, v in secrets_env.items() if v})
    return env


def check_settings(settings: Settings) -> Path:
    """The bot path, or MeetingError(501) saying what is missing."""
    bot = settings.meeting_bot_path
    if bot is None:
        raise MeetingError(501, "meetings are off: CONVO_MEETING_BOT_PATH is not set")
    if not (bot.is_file() and os.access(bot, os.X_OK)):
        raise MeetingError(501, f"CONVO_MEETING_BOT_PATH {bot} is not an executable file")
    if not (settings.zoom_sdk_key and settings.zoom_sdk_secret):
        raise MeetingError(501, "meetings need CONVO_ZOOM_SDK_KEY and CONVO_ZOOM_SDK_SECRET")
    if settings.meeting_bot_runner not in ("auto", "systemd", "plain"):
        raise MeetingError(501, "CONVO_MEETING_BOT_RUNNER must be auto, systemd or plain")
    if not _CPU.match(settings.meeting_bot_cpu):
        raise MeetingError(501, "CONVO_MEETING_BOT_CPU must look like 200%")
    try:
        memory_bytes(settings.meeting_bot_memory)
    except ValueError as e:
        raise MeetingError(501, str(e)) from e
    if settings.meeting_track_codec not in ("opus", "wav"):
        raise MeetingError(501, "CONVO_MEETING_TRACK_CODEC must be opus or wav")
    if settings.zoom_join_token not in ("auto", "zak", "obf", "none"):
        raise MeetingError(501, "CONVO_ZOOM_JOIN_TOKEN must be auto, zak, obf or none")
    for name, value in (("CONVO_MAX_SESSION_SECONDS", settings.max_session_seconds),
                        ("CONVO_MEETING_ALONE_SECONDS", settings.meeting_alone_seconds),
                        ("CONVO_MEETING_WAITING_ROOM_SECONDS",
                         settings.meeting_waiting_room_seconds)):
        if not (math.isfinite(value) and 1 <= int(value) <= MAX_BOT_SECONDS):
            raise MeetingError(501, f"{name} must be a whole number of seconds from 1 to "
                                    f"{MAX_BOT_SECONDS} for the meeting bot")
    return bot


def parse_state_line(line: str) -> tuple[str, str | None] | None:
    m = _STATE_LINE.match(line.strip())
    return (m.group(1), m.group(2)) if m else None


def final_state(last: tuple[str, str | None] | None, code: int,
                stop_reason: str | None) -> tuple[str, str]:
    """The row's state and reason once the bot exited with ``code``. The bot's own last
    ``ended``/``failed`` line wins, except its ``ended stopped``, which only says it got
    the server's SIGTERM: then the server's reason (``stopped``, ``max_duration``,
    ``server_stop``) says why. Else a stop the server asked for ends it; else exit code 0
    is ``ended``/``exited`` and anything else ``failed``/``bot_crashed``."""
    if last == ("ended", "stopped") and stop_reason:
        return "ended", stop_reason
    if last and last[0] in TERMINAL:
        return last[0], last[1] or ("exited" if last[0] == "ended" else "bot_crashed")
    if stop_reason:
        return "ended", stop_reason
    return ("ended", "exited") if code == 0 else ("failed", "bot_crashed")


def runtime_limit_kill(code: int, ran_for: float, max_seconds: float) -> bool:
    """Was the bot stopped by an outside runtime limit (systemd's ``RuntimeMaxSec``):
    killed by SIGTERM or SIGKILL after its max duration had passed?"""
    return code in (-signal.SIGTERM, -signal.SIGKILL) and ran_for >= max_seconds


class LineSplitter:
    """Bytes in, complete lines out. A line longer than MAX_LOG_LINE is dropped whole,
    up to and including its newline, so its tail is never read as a line of its own."""

    def __init__(self, limit: int = MAX_LOG_LINE) -> None:
        self.limit, self.buf, self.skipping = limit, b"", False

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        lines = []
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > self.limit:  # too long already: drop until its end
                    self.buf, self.skipping = b"", True
                return lines
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            if self.skipping:
                self.skipping = False
            elif len(line) <= self.limit:
                lines.append(line)

    def end(self) -> list[bytes]:
        """At EOF: a last line without a newline, unless it is being dropped."""
        line, self.buf = self.buf, b""
        return [line] if line and not self.skipping and len(line) <= self.limit else []


def out(row: dict[str, Any]) -> dict[str, Any]:
    """A meetings row as the API returns it."""
    def iso(ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None

    return {"id": row["id"], "state": row["state"], "reason": row["reason"],
            "active": row["state"] in MEETING_ACTIVE, "session_id": row["session_id"],
            "meeting_id": row["meeting_id"], "title": row["title"],
            "join_token_kind": row["join_token_kind"], "created_at": iso(row["created_at"]),
            "started_at": iso(row["started_at"]),
            "ended_at": iso(row["ended_at"])}


def _alive(pid: int | None, session_id: str) -> bool:
    """Is ``pid`` still the bot of ``session_id`` (not a reused pid)?"""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:  # no /proc: only ask whether the pid exists
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return session_id.encode() in cmdline.split(b"\0")


# ------------------------------------------------------------------ launcher
@dataclass
class _Running:
    id: str
    session_id: str
    proc: subprocess.Popen[bytes]
    runner: str
    unit: str
    work_dir: Path
    exited: asyncio.Future[int]
    done: asyncio.Event = field(default_factory=asyncio.Event)  # watcher task over
    finished: asyncio.Event = field(default_factory=asyncio.Event)  # final row written
    started: float = field(default_factory=time.monotonic)
    last: tuple[str, str | None] | None = None
    stop_reason: str | None = None
    task: asyncio.Task[None] | None = None


class Launcher:
    """Starts, tracks and stops meeting bots for one API app (``app.state.meetings``)."""

    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline
        self.settings: Settings = pipeline.settings
        self.store: Store = pipeline.store
        self.running: dict[str, _Running] = {}
        self.stop_grace = STOP_GRACE_SECONDS
        self.zoom_transport: Any = None  # tests: a fake Zoom API (httpx transport)
        self._runner: str | None = None
        self._join_lock = asyncio.Lock()  # one join at a time: busy check + token fetch

    # ------------------------------------------------------------- requests
    async def join(self, url: str | None, meeting_id: str | int | None,
                   passcode: str | None, title: str | None) -> dict[str, Any]:
        async with self._join_lock:
            return await self._join(url, meeting_id, passcode, title)

    async def _join(self, url: str | None, meeting_id: str | int | None,
                    passcode: str | None, title: str | None) -> dict[str, Any]:
        bot = check_settings(self.settings)
        try:
            meeting = parse_join_request(url, meeting_id, passcode)
        except ValueError as e:
            raise MeetingError(400, str(e)) from e
        title = " ".join((title or "").split()) or None
        if title and len(title) > MAX_TITLE_CHARS:
            raise MeetingError(400, f"title is longer than {MAX_TITLE_CHARS} characters")
        self.reconcile()
        active = self.store.active_meetings()
        if len(active) >= self.settings.meeting_bot_max:
            n = self.settings.meeting_bot_max
            raise MeetingError(409, "busy", active=active[-1]["id"],
                               message=f"Athena is already in a meeting ({len(active)} of "
                                       f"{n} bot{'s' if n != 1 else ''}). Stop it first "
                                       "(DELETE /v1/meetings/<id>) or wait until it ends.")
        # the only network call: Zoom's API, for the join token (before anything starts)
        join_kind, join_token = await join_token_provider(
            self.settings, meeting, transport=self.zoom_transport)
        runner = await self._pick_runner()
        mid = uuid.uuid4().hex
        session_id = f"zoom-{mid}"
        unit = f"athena-meeting-{mid}"
        work_dir = self.settings.data_dir / "meetings" / mid
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.store.add_meeting({
            "id": mid, "state": "starting", "session_id": session_id,
            "meeting_id": meeting.meeting_id, "title": title, "runner": runner,
            "join_token_kind": join_kind,
            "unit": unit if runner == "systemd" else None,
            "upload_token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": now})
        assert self.settings.zoom_sdk_key and self.settings.zoom_sdk_secret
        env = bot_env({
            "ATHENA_MEETING_JWT": sign_sdk_jwt(
                self.settings.zoom_sdk_key, self.settings.zoom_sdk_secret,
                meeting.meeting_id,
                lifetime=self.settings.max_session_seconds + 3600),
            "ATHENA_UPLOAD_TOKEN": token,
            "ATHENA_MEETING_PASSCODE": meeting.passcode or "",
            "ATHENA_ZOOM_JOIN_TOKEN": join_token or "",
            "ATHENA_ZOOM_JOIN_TOKEN_KIND": join_kind if join_token else "",
        })
        argv = build_command(self.settings, runner, bot,
                             bot_args(self.settings, meeting, session_id, work_dir,
                                      join_token=join_token is not None), unit)
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    env=env, cwd=work_dir, close_fds=True,
                                    start_new_session=True)
        except OSError as e:
            log.error("meeting %s: could not start the bot: %s", mid, e)
            shutil.rmtree(work_dir, ignore_errors=True)
            self._finish_row(mid, session_id, "failed", "bot_not_started")
            raise MeetingError(500, f"could not start the meeting bot: {e}") from e
        if runner == "plain":
            self._cap_plain(proc.pid)
        self.store.update_meeting(mid, pid=proc.pid)
        loop = asyncio.get_running_loop()
        run = _Running(mid, session_id, proc, runner, unit, work_dir, loop.create_future())
        self.running[mid] = run
        run.task = loop.create_task(self._watch(run), name=f"meeting-{mid}")
        log.info("meeting %s: bot started (pid %d, %s runner) for Zoom meeting %s",
                 mid, proc.pid, runner, meeting.meeting_id)
        return {"id": mid, "state": "starting", "session_id": session_id}

    def get(self, mid: str) -> dict[str, Any]:
        self.reconcile()
        row = self.store.get_meeting(mid)
        if row is None:
            raise MeetingError(404, "meeting not found")
        return out(row)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        self.reconcile()
        return [out(r) for r in self.store.list_meetings(max(1, min(limit, 100)))]

    async def leave(self, mid: str) -> dict[str, Any]:
        """Ask the bot to leave (SIGTERM; SIGKILL after the grace) and wait for it."""
        self.reconcile()
        row = self.store.get_meeting(mid)
        if row is None:
            raise MeetingError(404, "meeting not found")
        if row["state"] in MEETING_ACTIVE:
            run = self.running.get(mid)
            if run is not None:
                await self._terminate(run, "stopped")  # returns once the bot exited
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run.finished.wait(), READER_DRAIN_SECONDS + 3.0)
                exited = run.exited.done() and not run.exited.cancelled()
                if exited and not run.finished.is_set():  # never "active" for a dead bot
                    self._finish_run(run, run.exited.result())
            else:  # a bot started before a server restart
                await self._stop_orphan(row)
        return out(self.store.get_meeting(mid) or row)

    async def shutdown(self) -> None:
        """Server shutdown: stop watching (bots keep running, see reconcile). Except
        when this process is its PID namespace's init (the sound-api container, whose
        entrypoint execs convo-server): its exit SIGKILLs every bot, losing their last
        chunks, the meeting's chat and the session's end, so they are asked to leave
        first (SIGTERM; final rows ``ended server_stop``)."""
        runs = list(self.running.values())
        if runs and _exit_kills_bots():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(
                    *(self._terminate(r, "server_stop") for r in runs)),
                    SHUTDOWN_STOP_SECONDS)
            tasks = [r.task for r in runs if r.task]
            if tasks:  # their final rows
                await asyncio.wait(tasks, timeout=READER_DRAIN_SECONDS + 0.5)
        for run in list(self.running.values()):
            if run.task:
                run.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run.task

    # ------------------------------------------------------------ internals
    def reconcile(self) -> None:
        """Rows left active by an earlier server process: still that bot? Else failed
        ``server_restarted``. Runs on meeting requests only."""
        for row in self.store.active_meetings():
            if row["id"] in self.running:
                continue
            if not _alive(row["pid"], row["session_id"]):
                log.warning("meeting %s: bot is gone after a server restart", row["id"])
                shutil.rmtree(self.settings.data_dir / "meetings" / row["id"],
                              ignore_errors=True)
                self._finish_row(row["id"], row["session_id"], "failed", "server_restarted")

    async def _pick_runner(self) -> str:
        want = self.settings.meeting_bot_runner
        if want != "auto":
            return want
        if self._runner is None:  # probed once per server process, on the first join
            ok = False
            if shutil.which("systemd-run"):
                code = await _run_quick(["systemd-run", "--user", "--scope", "--quiet",
                                         "--collect", "true"], bot_env({}))
                ok = code == 0
            self._runner = "systemd" if ok else "plain"
            log.info("meeting bots use the %s runner", self._runner)
        return self._runner

    def _cap_plain(self, pid: int) -> None:
        import resource

        limit = memory_bytes(self.settings.meeting_bot_memory)
        try:
            resource.prlimit(pid, resource.RLIMIT_AS, (limit, limit))
        except (OSError, ValueError) as e:  # the bot may already have exited
            log.warning("could not cap the meeting bot's memory: %s", e)
        if not shutil.which("nice"):
            with contextlib.suppress(OSError):
                os.setpriority(os.PRIO_PROCESS, pid, 10)

    async def _watch(self, run: _Running) -> None:
        loop = asyncio.get_running_loop()
        _watch_exit(loop, run.proc, run.exited)
        reader = loop.create_task(self._read(run))
        lifetime = self.settings.max_session_seconds + LIFETIME_GRACE_SECONDS
        try:
            try:
                await asyncio.wait_for(asyncio.shield(run.exited), lifetime)
            except TimeoutError:
                log.warning("meeting %s: bot outlived its max duration", run.id)
                await self._terminate(run, "max_duration")
            code = await run.exited
            # helpers the bot left in its process group go too (they may hold stdout)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(run.proc.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(reader, READER_DRAIN_SECONDS)
            except TimeoutError:
                reader.cancel()
            if run.stop_reason is None and runtime_limit_kill(
                    code, time.monotonic() - run.started, self.settings.max_session_seconds):
                run.stop_reason = "max_duration"  # systemd's RuntimeMaxSec, not a crash
            # the row first (DELETE answers with it), then the slower scope cleanup
            self._finish_run(run, code)
            if run.runner == "systemd":
                await _run_quick(["systemctl", "--user", "stop", f"{run.unit}.scope"],
                                 bot_env({}))
        except asyncio.CancelledError:
            reader.cancel()
            _unwatch_exit(loop, run.exited)
            raise
        finally:
            with contextlib.suppress(Exception):
                if run.proc.stdout:
                    run.proc.stdout.close()
            if run.proc.returncode is not None:
                shutil.rmtree(run.work_dir, ignore_errors=True)
            self.running.pop(run.id, None)
            run.done.set()

    async def _read(self, run: _Running) -> None:
        loop = asyncio.get_running_loop()
        stream = asyncio.StreamReader(limit=2 * MAX_LOG_LINE, loop=loop)
        assert run.proc.stdout is not None
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stream, loop=loop), run.proc.stdout)
        lines = LineSplitter()
        try:
            while True:
                data = await stream.read(MAX_LOG_LINE)
                for raw in lines.feed(data) if data else lines.end():
                    parsed = parse_state_line(raw.decode("utf-8", "replace"))
                    if parsed is not None:
                        self._on_state(run, *parsed)
                if not data:
                    return
        finally:
            transport.close()

    def _on_state(self, run: _Running, state: str, reason: str | None) -> None:
        if run.last and run.last[0] in TERMINAL:
            return
        run.last = (state, reason)
        if state in TERMINAL:
            return  # written when the bot exits
        fields: dict[str, Any] = {"state": state, "reason": reason}
        if state == "recording":
            row = self.store.get_meeting(run.id)
            if row and row["started_at"] is None:
                fields["started_at"] = time.time()
        self.store.update_meeting(run.id, **fields)
        log.info("meeting %s: %s%s", run.id, state, f" ({reason})" if reason else "")

    async def _terminate(self, run: _Running, reason: str) -> None:
        if run.exited.done():  # gone already: its exit code says why, not this stop
            return
        if run.stop_reason is None:
            run.stop_reason = reason
            self.store.update_meeting(run.id, stop_requested_at=time.time())
        with contextlib.suppress(ProcessLookupError):
            run.proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(run.exited), self.stop_grace)
        except TimeoutError:
            log.warning("meeting %s: bot ignored SIGTERM, killing it", run.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(run.proc.pid, signal.SIGKILL)
            await asyncio.shield(run.exited)

    async def _stop_orphan(self, row: dict[str, Any]) -> None:
        pid, sid = row["pid"], row["session_id"]
        self.store.update_meeting(row["id"], stop_requested_at=time.time())
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_grace
        while _alive(pid, sid) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if _alive(pid, sid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        if row["unit"]:
            await _run_quick(["systemctl", "--user", "stop", f"{row['unit']}.scope"],
                             bot_env({}))
        shutil.rmtree(self.settings.data_dir / "meetings" / row["id"], ignore_errors=True)
        self._finish_row(row["id"], sid, "ended", "stopped")

    def _finish_run(self, run: _Running, code: int) -> None:
        """The final row of a bot that exited with ``code``, written once."""
        if run.finished.is_set():
            return
        state, reason = final_state(run.last, code, run.stop_reason)
        log.info("meeting %s: bot exited (code %s): %s %s", run.id, code, state, reason)
        self._finish_row(run.id, run.session_id, state, reason)
        run.finished.set()

    def _finish_row(self, mid: str, session_id: str, state: str, reason: str) -> None:
        self.store.update_meeting(mid, state=state, reason=reason, ended_at=time.time(),
                                  upload_token_hash=None, pid=None)
        # the bot normally ends its session itself; make sure it is ended
        self.pipeline.end_session(session_id, f"meeting_{reason}")


def _exit_kills_bots() -> bool:
    """Is this process its PID namespace's init (PID 1 in a container)? When it exits,
    the kernel SIGKILLs every other process of the namespace, bots included."""
    return os.getpid() == 1


# --------------------------------------------------- child exit, no thread
def _watch_exit(loop: asyncio.AbstractEventLoop, proc: subprocess.Popen[bytes],
                fut: asyncio.Future[int]) -> None:
    """Resolve ``fut`` with the exit code when ``proc`` exits, reaping it. Uses a pidfd
    on the event loop (Linux 5.3+): no thread. Without pidfd: a 0.5 s check on the loop,
    only while the bot runs."""
    def reap() -> bool:
        code = proc.poll()
        if code is None:
            return False
        if not fut.done():
            fut.set_result(code)
        return True

    try:
        pidfd = os.pidfd_open(proc.pid)
    except (AttributeError, OSError):
        async def poll() -> None:
            while not reap():
                await asyncio.sleep(0.5)
        task = loop.create_task(poll())
        fut.add_done_callback(lambda _: task.cancel())
        return

    # the done callback removes the reader and closes the pidfd, exactly once
    loop.add_reader(pidfd, reap)
    fut.add_done_callback(lambda _: _close_pidfd(loop, pidfd))
    reap()  # it may have exited before the reader was added


def _close_pidfd(loop: asyncio.AbstractEventLoop, pidfd: int) -> None:
    with contextlib.suppress(Exception):
        loop.remove_reader(pidfd)
    with contextlib.suppress(OSError):
        os.close(pidfd)


def _unwatch_exit(loop: asyncio.AbstractEventLoop, fut: asyncio.Future[int]) -> None:
    if not fut.done():
        fut.cancel()  # its done callback closes the pidfd


async def _run_quick(argv: list[str], env: dict[str, str], timeout: float = 5.0) -> int:
    """Run a short helper (systemd probe, scope stop) without blocking the loop or
    starting a thread; its exit code, or -1."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, close_fds=True)
    except OSError:
        return -1
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[int] = loop.create_future()
    _watch_exit(loop, proc, fut)
    try:
        return await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        proc.kill()
        proc.wait()
        return -1
