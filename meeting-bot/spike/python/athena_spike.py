#!/usr/bin/env python3
"""Athena Zoom bot, Phase 0 spike: the same bot as ../cpp through a Python binding.

UNVERIFIED: written without the Zoom SDK, a Zoom account or a meeting. It targets
`py-zoom-meeting-sdk` (import name `zoom_meeting_sdk`, github.com/noah-duncan/
py-zoom-meeting-sdk), whose names mirror the C++ SDK; the names below were read from
that project's sample bot, not run. Fix what differs and note it in
docs/meeting-bot-spike.md.

Build the binding from source against the owner's SDK folder (see the doc's Owner
steps). Don't `pip install zoom-meeting-sdk` from PyPI for the spike: those wheels bundle
a copy of Zoom's proprietary libmeetingsdk, not the one from the owner's account.

Same behaviour as the C++ spike: joins as "Athena" with video off, never subscribes to
video or share, records whatever per-participant raw audio Zoom delivers (one PCM file
per Zoom user id plus participants.json), logs chat to chat.jsonl, leaves after
--minutes, when the meeting ends, when removed, or on SIGINT/SIGTERM. Sends Zoom's
recording request once and waits (`STATE waiting_permission`, `waiting_permission
denied`, then `recording` on a grant, now or later); never asks again, never leaves over
it, never posts in chat. Prints the same STATE lines.

    ATHENA_SPIKE_JWT=$(python3 ../sign_jwt.py) python3 athena_spike.py \
        --meeting-id 81234567890 --passcode xxxxxx --minutes 30 --out out-py

Meetings in other accounts: set ATHENA_ZOOM_JOIN_TOKEN to an on-behalf-of token from the
owner's own account (ATHENA_ZOOM_JOIN_TOKEN_KIND=obf, the default). Zoom admits the bot
only while the owner is in the meeting: it prints `STATE starting waiting_for_owner` while
it retries, `STATE failed owner_not_joined` after --owner-wait-seconds, and `STATE ended
owner_left` when Zoom drops it because he left. Later option: a separate Athena account's
ZAK (ATHENA_ZOOM_JOIN_TOKEN_KIND=zak).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import gi  # PyGObject: the SDK delivers callbacks on a GLib main loop

gi.require_version("GLib", "2.0")
import zoom_meeting_sdk as zoom  # noqa: E402
from gi.repository import GLib  # noqa: E402


def say(state: str, _last: list[str] = [""]) -> None:  # noqa: B006 (deliberate memo)
    if state != _last[0]:
        _last[0] = state
        print(f"STATE {state}", flush=True)


def log(msg: str) -> None:
    print(f"LOG {msg}", flush=True)


def join_token() -> tuple[str, str]:
    kind = os.environ.get("ATHENA_ZOOM_JOIN_TOKEN_KIND", "") or "obf"
    if kind not in ("obf", "zak"):
        raise SystemExit("ATHENA_ZOOM_JOIN_TOKEN_KIND must be obf or zak")
    return os.environ.get("ATHENA_ZOOM_JOIN_TOKEN", ""), kind


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Bot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.loop = GLib.MainLoop()
        self.tracks: dict[int, dict] = {}
        self.files: dict[int, object] = {}
        self.rate = 0
        self.channels = 1
        self.self_id = 0
        self.raw_started = 0.0  # time recording started; 0 while waiting for permission
        self.requested = False
        self.denied = False
        self.last_can = None
        self.leaving = False
        self.failed = False
        self.end_reason = "left"
        self.joined_at = 0.0
        self.first_join = 0.0
        self.in_meeting = False
        self.last_audio = 0.0
        self.chat_file = None
        # keep references: the binding does not own callback objects
        self.keep: list[object] = []

    # --- SDK setup -------------------------------------------------------------------
    def start(self) -> None:
        say("starting")
        init = zoom.InitParam()
        init.strWebDomain = "https://zoom.us"
        init.strSupportUrl = "https://zoom.us"
        init.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
        init.enableLogByDefault = True
        init.enableGenerateDump = False
        if zoom.InitSDK(init) != zoom.SDKERR_SUCCESS:
            return self.fail("sdk_init")
        self.meeting = zoom.CreateMeetingService()
        self.settings = zoom.CreateSettingService()
        self.auth = zoom.CreateAuthService()
        ev = zoom.MeetingServiceEventCallbacks(onMeetingStatusChangedCallback=self.on_status)
        self.meeting.SetEvent(ev)
        auth_ev = zoom.AuthServiceEventCallbacks(onAuthenticationReturnCallback=self.on_auth)
        self.auth.SetEvent(auth_ev)
        self.keep += [ev, auth_ev]
        ctx = zoom.AuthContext()
        ctx.jwt_token = os.environ["ATHENA_SPIKE_JWT"]
        if self.auth.SDKAuth(ctx) != zoom.SDKERR_SUCCESS:
            return self.fail("sdk_auth_call")

    def on_auth(self, result) -> None:
        if result != zoom.AUTHRET_SUCCESS:
            return self.fail(f"sdk_auth_{result}")
        log("SDK auth ok")
        self.join()

    def join(self) -> bool:
        jp = zoom.JoinParam()
        jp.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN
        p = jp.param
        p.meetingNumber = int(self.args.meeting_id)
        p.userName = self.args.name
        p.psw = self.args.passcode
        p.isVideoOff = True
        p.isAudioOff = False
        p.isAudioRawDataStereo = False
        p.isMyVoiceInMix = False
        # ATHENA_ZOOM_JOIN_TOKEN (lane 1B's name): kind "obf" (default) = on behalf of the
        # owner, primary, only while the owner is in the meeting; "zak" = a separate Athena
        # account, later option. Empty: JWT only.
        token, kind = join_token()
        if token:
            setattr(p, "userZAK" if kind == "zak" else "onBehalfToken", token)
        log(f"joining with {kind if token else 'jwt only'}")
        if os.environ.get("ATHENA_SPIKE_RECORDING_TOKEN"):  # host-issued, optional
            p.app_privilege_token = os.environ["ATHENA_SPIKE_RECORDING_TOKEN"]
        self.settings.GetAudioSettings().EnableAutoJoinAudio(True)
        err = self.meeting.Join(jp)
        if err != zoom.SDKERR_SUCCESS:
            self.fail(f"join_call_{err}")
        return False  # also a one-shot GLib timeout callback

    def obf(self) -> bool:
        token, kind = join_token()
        return bool(token) and kind == "obf"

    def wait_for_owner(self) -> None:
        """OBF: Zoom refuses until the owner is in. Retry every 3 s up to the limit."""
        self.first_join = self.first_join or time.time()
        if time.time() - self.first_join >= self.args.owner_wait_seconds:
            return self.fail("owner_not_joined")
        say("starting waiting_for_owner")
        GLib.timeout_add_seconds(3, self.join)

    def owner_left(self, code) -> None:
        log(f"session ended by Zoom, code {code} (obf: taken as the owner leaving; record it)")
        self.end_reason = "owner_left"
        self.leaving = True
        self.loop.quit()

    # --- meeting ---------------------------------------------------------------------
    def on_status(self, status, result) -> None:
        log(f"meeting status {status} result {result}")
        if status == zoom.MEETING_STATUS_IN_WAITING_ROOM:
            say("waiting_room")
        elif status == zoom.MEETING_STATUS_INMEETING:
            self.on_joined()
        elif status == zoom.MEETING_STATUS_FAILED:
            if self.in_meeting and self.obf() and not self.leaving:
                self.owner_left(result)
            elif (result == zoom.MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING  # SDK >= 6.6.10
                  and not self.leaving):
                self.wait_for_owner()
            else:
                self.fail(f"join_failed_{result}")
        elif status == zoom.MEETING_STATUS_ENDED:
            if not self.leaving:
                if result == zoom.EndMeetingReason_KickByHost:
                    self.end_reason = "removed"
                elif result == zoom.EndMeetingReason_EndByHost:
                    self.end_reason = "meeting_ended"
                elif self.obf():
                    return self.owner_left(result)
                else:
                    self.end_reason = f"meeting_ended_{result}"
            self.leaving = True
            self.loop.quit()

    def on_joined(self) -> None:
        self.in_meeting = True
        if self.joined_at:  # back in after a reconnect: timers and events already set
            return say("recording") if self.raw_started else self.say_waiting()
        self.joined_at = time.time()
        self.participants = self.meeting.GetMeetingParticipantsController()
        self.self_id = self.participants.GetMySelfUser().GetUserID()
        self.chat_file = open(self.out / "chat.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        chat_ev = zoom.MeetingChatEventCallbacks(onChatMsgNotificationCallback=self.on_chat)
        self.meeting.GetMeetingChatController().SetEvent(chat_ev)
        rec_ev = zoom.MeetingRecordingCtrlEventCallbacks(
            onRecordPrivilegeChangedCallback=self.on_privilege,
            onLocalRecordingPrivilegeRequestStatusCallback=self.on_request_answer)
        self.meeting.GetMeetingRecordingController().SetEvent(rec_ev)
        self.keep += [chat_ev, rec_ev]
        log(f"JoinVoip: {self.meeting.GetMeetingAudioController().JoinVoip()}")
        self.start_or_ask()
        GLib.timeout_add_seconds(5, self.audio_watch)
        GLib.timeout_add_seconds(self.args.minutes * 60, self.leave, "duration")

    # Recording permission (owner's decision): ask Zoom ONCE, then wait; never ask again
    # and never leave over it.
    def say_waiting(self) -> None:
        say("waiting_permission denied" if self.denied else "waiting_permission")

    def start_or_ask(self) -> None:
        if self.try_start_raw_audio():
            return
        if not self.requested:
            self.requested = True
            rec = self.meeting.GetMeetingRecordingController()
            log(f"recording request sent: {rec.RequestLocalRecordingPrivilege()}")
        self.say_waiting()
        # fallback in case the privilege callback never fires (reported on Zoom's forum)
        GLib.timeout_add_seconds(15, lambda: not (self.leaving or self.try_start_raw_audio()))

    def on_privilege(self, can_record: bool) -> None:
        log(f"record privilege changed: {can_record}")
        if can_record:
            self.try_start_raw_audio()

    def on_request_answer(self, status) -> None:
        log(f"recording request answer: {status}")
        if status == zoom.RequestLocalRecordingStatus.RequestLocalRecording_Granted:
            self.try_start_raw_audio()
        elif (status == zoom.RequestLocalRecordingStatus.RequestLocalRecording_Denied
              and not self.raw_started):
            self.denied = True
            self.say_waiting()  # keep waiting: the host may still allow it later

    def try_start_raw_audio(self) -> bool:
        if self.raw_started:
            return True
        if self.leaving:
            return False
        rec = self.meeting.GetMeetingRecordingController()
        can = rec.CanStartRawRecording()
        if can != zoom.SDKERR_SUCCESS:
            if can != self.last_can:
                self.last_can = can
                log(f"raw audio not allowed yet ({can})")
            return False
        if (err := rec.StartRawRecording()) != zoom.SDKERR_SUCCESS:
            log(f"StartRawRecording failed: {err}")
            return False
        self.audio_helper = zoom.GetAudioRawdataHelper()
        self.audio_delegate = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
            onOneWayAudioRawDataReceivedCallback=self.on_audio)
        if (err := self.audio_helper.subscribe(self.audio_delegate, False)) != zoom.SDKERR_SUCCESS:
            log(f"audio subscribe failed: {err}")
            return False
        self.raw_started = time.time()
        log("raw audio subscribed")
        say("recording")
        return True

    def audio_watch(self) -> bool:
        """Every 5 s while in the meeting: are audio frames arriving once recording?"""
        if self.leaving:
            return False
        if self.raw_started:
            quiet = time.time() - max(self.last_audio, self.raw_started)
            say("recording no_audio" if quiet >= self.args.no_audio_seconds else "recording")
        return True

    # --- data ------------------------------------------------------------------------
    def on_audio(self, data, user_id: int) -> None:
        if user_id == self.self_id:
            return
        self.last_audio = time.time()
        buf = data.GetBuffer()
        if user_id not in self.files:
            self.rate = data.GetSampleRate()
            self.channels = data.GetChannelNum()
            name = self.user_name(user_id)
            self.files[user_id] = open(self.out / f"track-{user_id}.pcm", "ab")  # noqa: SIM115
            self.tracks[user_id] = {"name": name, "file": f"track-{user_id}.pcm", "bytes": 0,
                                    "first_audio": iso(self.last_audio)}
            log(f"new track {user_id} {json.dumps(name)} {self.rate} Hz")
            self.write_sidecar()
        self.files[user_id].write(buf)
        self.tracks[user_id]["bytes"] += len(buf)

    def user_name(self, user_id: int) -> str:
        user = self.participants.GetUserByUserID(user_id)
        return (user.GetUserName() if user else "") or ""

    def on_chat(self, msg, _content) -> None:
        # the binding passes content=None; read it from the message (its sample says so)
        row = {"at": iso(msg.GetTimeStamp()), "from": msg.GetSenderDisplayName() or "",
               "zoom_user_id": str(msg.GetSenderUserId()), "text": msg.GetContent() or "",
               "to_all": bool(msg.IsChatToAll())}
        self.chat_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.chat_file.flush()
        log("chat message logged")

    def write_sidecar(self) -> None:
        doc = {"format": "s16le", "sample_rate": self.rate, "channels": self.channels,
               "tracks": {str(k): v for k, v in self.tracks.items()}}
        tmp = self.out / "participants.json.tmp"
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.out / "participants.json")

    # --- leaving ---------------------------------------------------------------------
    def leave(self, reason: str) -> bool:
        if self.leaving:
            return False
        self.leaving = True
        self.end_reason = reason
        log(f"leaving: {reason}")
        if self.meeting.GetMeetingStatus() != zoom.MEETING_STATUS_IDLE:
            self.meeting.Leave(zoom.LEAVE_MEETING)
            GLib.timeout_add_seconds(10, lambda: self.loop.quit() or False)
        else:
            self.loop.quit()
        return False

    def fail(self, reason: str) -> None:
        self.failed = True
        say(f"failed {reason}")
        if hasattr(self, "meeting"):
            self.leave(reason)
        else:
            self.loop.quit()

    def cleanup(self) -> None:
        if self.raw_started:
            self.audio_helper.unSubscribe()
        for uid, f in self.files.items():
            f.close()
            name = self.user_name(uid)  # pick up renames; keep the old name if they left
            if name:
                self.tracks[uid]["name"] = name
        if self.files:
            self.write_sidecar()
        if self.chat_file:
            self.chat_file.close()
        for destroy, svc in ((zoom.DestroyAuthService, "auth"),
                             (zoom.DestroySettingService, "settings"),
                             (zoom.DestroyMeetingService, "meeting")):
            if hasattr(self, svc):
                destroy(getattr(self, svc))
        zoom.CleanUPSDK()


def main() -> int:
    ap = argparse.ArgumentParser(description="Athena Zoom spike (Python binding)")
    ap.add_argument("--meeting-id", required=True)
    ap.add_argument("--passcode", default="")
    ap.add_argument("--name", default="Athena")
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--out", default="out-py")
    ap.add_argument("--no-audio-seconds", type=int, default=60)
    ap.add_argument("--owner-wait-seconds", type=int, default=600)
    args = ap.parse_args()
    join_token()  # fail early on a bad kind
    if not os.environ.get("ATHENA_SPIKE_JWT"):
        print("set ATHENA_SPIKE_JWT (python3 ../sign_jwt.py)", file=sys.stderr)
        return 2
    bot = Bot(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, bot.leave, "stopped")
    GLib.idle_add(lambda: bot.start() or False)
    bot.loop.run()
    bot.cleanup()
    if not bot.failed:
        say(f"ended {bot.end_reason}")
    return 1 if bot.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
