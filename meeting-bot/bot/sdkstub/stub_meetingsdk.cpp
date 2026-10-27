// SANDBOX-ONLY FAKE of the Zoom Meeting SDK, written by Athena (not Zoom's code). Built as
// libmeetingsdk.so only when -DZOOM_SDK_DIR points at meeting-bot/bot/sdkstub, so the bot
// compiles, links and runs in a sandbox or CI without the proprietary SDK.
//
// It simulates one fictional meeting on the GLib main loop the bot runs. Pick the
// scenario with ATHENA_SDKSTUB_SCENARIO (stub only; the real SDK ignores it):
//   record (default)  in the meeting, may record at once; Dana talks; chat; Lee leaves
//   permission        may not record until the one request is granted (after 1 s)
//   denied            the request is declined; never granted
//   ended / removed   the host ends the meeting / removes the bot after 3 s
//   alone             nobody else in the meeting
//   waiting_room      left in the waiting room
//   no_host           waiting for the host forever
//   wrong_passcode    join fails with a wrong passcode
//   owner_late        obf: owner joins after two refused attempts
//   no_owner          obf: owner never joins
//   owner_left        obf: in the meeting, Zoom drops the bot after 3 s
//   auth_fail         SDK auth refused
// All names and data are fictional.
#include <glib.h>

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "athena_sdkstub.h"

namespace ZOOMSDK {
namespace {

std::string scenario() {
  const char* s = std::getenv("ATHENA_SDKSTUB_SCENARIO");
  return s && *s ? s : "record";
}

struct User : IUserInfo {
  unsigned int id;
  std::string name;
  User(unsigned int i, std::string n) : id(i), name(std::move(n)) {}
  const zchar_t* GetUserName() override { return name.c_str(); }
  unsigned int GetUserID() override { return id; }
};

User g_self(16778240, "Athena");
std::vector<User*> g_users;  // others in the meeting
User g_dana(16779264, "Dana Example");
User g_lee(16780288, "Lee Sample");

struct IdList : IList<unsigned int> {
  std::vector<unsigned int> ids;
  int GetCount() override { return static_cast<int>(ids.size()); }
  unsigned int GetItem(int i) override { return ids.at(i); }
};
IdList g_list;

struct Participants : IMeetingParticipantsController {
  IList<unsigned int>* GetParticipantsList() override {
    g_list.ids = {g_self.id};
    for (auto* u : g_users) g_list.ids.push_back(u->id);
    return &g_list;
  }
  IUserInfo* GetUserByUserID(unsigned int id) override {
    if (id == g_self.id) return &g_self;
    for (auto* u : g_users)
      if (u->id == id) return u;
    return nullptr;
  }
  IUserInfo* GetMySelfUser() override { return &g_self; }
} g_participants;

struct Audio : IMeetingAudioController {
  SDKError JoinVoip() override { return SDKERR_SUCCESS; }
} g_audio_ctrl;

struct Msg : IChatMsgInfo {
  unsigned int from;
  std::string name, text;
  bool all;
  unsigned int GetSenderUserId() override { return from; }
  const zchar_t* GetSenderDisplayName() override { return name.c_str(); }
  const zchar_t* GetContent() override { return text.c_str(); }
  time_t GetTimeStamp() override { return time(nullptr); }
  bool IsChatToAll() override { return all; }
};

IMeetingChatCtrlEvent* g_chat_ev = nullptr;
struct Chat : IMeetingChatController {
  SDKError SetEvent(IMeetingChatCtrlEvent* ev) override {
    g_chat_ev = ev;
    return SDKERR_SUCCESS;
  }
} g_chat;

IMeetingRecordingCtrlEvent* g_rec_ev = nullptr;
bool g_may_record = false;
struct Recording : IMeetingRecordingController {
  SDKError SetEvent(IMeetingRecordingCtrlEvent* ev) override {
    g_rec_ev = ev;
    return SDKERR_SUCCESS;
  }
  SDKError CanStartRawRecording() override {
    return g_may_record ? SDKERR_SUCCESS : SDKERR_NO_PERMISSION;
  }
  SDKError StartRawRecording() override {
    return g_may_record ? SDKERR_SUCCESS : SDKERR_NO_PERMISSION;
  }
  SDKError RequestLocalRecordingPrivilege() override;
} g_recording;

struct Info : IMeetingInfo {
  const zchar_t* GetMeetingTopic() override { return "Q4 budget review (fictional)"; }
} g_info;

IMeetingServiceEvent* g_meeting_ev = nullptr;
MeetingStatus g_status = MEETING_STATUS_IDLE;
int g_join_attempts = 0;
std::vector<guint> g_sources;

void later(guint ms, GSourceFunc fn, gpointer data = nullptr) {
  g_sources.push_back(g_timeout_add(ms, fn, data));
}

void set_status(MeetingStatus s, int result) {
  g_status = s;
  if (g_meeting_ev) g_meeting_ev->onMeetingStatusChanged(s, result);
}

gboolean grant_cb(gpointer) {
  g_may_record = true;
  if (g_rec_ev) g_rec_ev->onLocalRecordingPrivilegeRequestStatus(RequestLocalRecording_Granted);
  return G_SOURCE_REMOVE;
}
gboolean deny_cb(gpointer) {
  if (g_rec_ev) g_rec_ev->onLocalRecordingPrivilegeRequestStatus(RequestLocalRecording_Denied);
  return G_SOURCE_REMOVE;
}

SDKError Recording::RequestLocalRecordingPrivilege() {
  std::string s = scenario();
  if (s == "permission") later(1000, grant_cb);
  if (s == "denied") later(500, deny_cb);
  return SDKERR_SUCCESS;
}

gboolean chat_cb(gpointer) {
  if (!g_chat_ev) return G_SOURCE_REMOVE;
  Msg m;
  m.from = g_dana.id;
  m.name = g_dana.name;
  m.text = "Here's the sheet: https://example.invalid/budget-v3.xlsx";
  m.all = true;
  g_chat_ev->onChatMsgNotification(&m, m.text.c_str());
  Msg p = m;
  p.text = "private note to Athena (must not be stored)";
  p.all = false;
  g_chat_ev->onChatMsgNotification(&p, p.text.c_str());
  return G_SOURCE_REMOVE;
}

gboolean lee_leaves_cb(gpointer) {
  for (size_t i = 0; i < g_users.size(); ++i)
    if (g_users[i] == &g_lee) g_users.erase(g_users.begin() + i);
  return G_SOURCE_REMOVE;
}

gboolean end_cb(gpointer data) {
  set_status(MEETING_STATUS_ENDED, GPOINTER_TO_INT(data));
  return G_SOURCE_REMOVE;
}
gboolean drop_cb(gpointer) {
  set_status(MEETING_STATUS_FAILED, 0);
  return G_SOURCE_REMOVE;
}

gboolean in_meeting_cb(gpointer) {
  std::string s = scenario();
  if (s != "alone") g_users = {&g_dana, &g_lee};
  g_may_record = s == "record" || s == "ended" || s == "removed" || s == "alone" ||
                 s == "owner_late" || s == "owner_left";
  set_status(MEETING_STATUS_INMEETING, 0);
  if (s != "alone") {
    later(1000, chat_cb);
    later(2500, lee_leaves_cb);
  }
  if (s == "ended") later(3000, end_cb, GINT_TO_POINTER(EndMeetingReason_EndByHost));
  if (s == "removed") later(3000, end_cb, GINT_TO_POINTER(EndMeetingReason_KickByHost));
  if (s == "owner_left") later(3000, drop_cb);
  return G_SOURCE_REMOVE;
}

gboolean join_cb(gpointer) {
  std::string s = scenario();
  set_status(MEETING_STATUS_CONNECTING, 0);
  if (s == "wrong_passcode") {
    set_status(MEETING_STATUS_FAILED, MEETING_FAIL_PASSWORD_ERR);
  } else if (s == "no_owner" || (s == "owner_late" && g_join_attempts <= 2)) {
    set_status(MEETING_STATUS_FAILED, MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING);
  } else if (s == "waiting_room") {
    set_status(MEETING_STATUS_IN_WAITING_ROOM, 0);
  } else if (s == "no_host") {
    set_status(MEETING_STATUS_WAITINGFORHOST, 0);
  } else {
    later(200, in_meeting_cb);
  }
  return G_SOURCE_REMOVE;
}

// Fake audio: Dana speaks a 220 Hz tone, 10 ms blocks at 32 kHz mono s16le.
struct Block : AudioRawData {
  std::vector<int16_t> pcm;
  char* GetBuffer() override { return reinterpret_cast<char*>(pcm.data()); }
  unsigned int GetBufferLen() override { return static_cast<unsigned>(pcm.size() * 2); }
  unsigned int GetSampleRate() override { return 32000; }
  unsigned int GetChannelNum() override { return 1; }
};
IZoomSDKAudioRawDataDelegate* g_audio_delegate = nullptr;
guint g_audio_source = 0;
uint64_t g_phase = 0;

gboolean audio_cb(gpointer) {
  if (!g_audio_delegate || g_status != MEETING_STATUS_INMEETING) return G_SOURCE_CONTINUE;
  Block b;
  b.pcm.resize(320);
  for (auto& v : b.pcm) v = static_cast<int16_t>(6000 * std::sin(2 * M_PI * 220 * (g_phase++) / 32000.0));
  g_audio_delegate->onOneWayAudioRawDataReceived(&b, g_dana.id);
  g_audio_delegate->onOneWayAudioRawDataReceived(&b, g_self.id);  // the bot hears itself: ignored
  return G_SOURCE_CONTINUE;
}

struct AudioHelper : IZoomSDKAudioRawDataHelper {
  SDKError subscribe(IZoomSDKAudioRawDataDelegate* d, bool) override {
    g_audio_delegate = d;
    if (!g_audio_source) g_audio_source = g_timeout_add(10, audio_cb, nullptr);
    return SDKERR_SUCCESS;
  }
  SDKError unSubscribe() override {
    g_audio_delegate = nullptr;
    return SDKERR_SUCCESS;
  }
} g_audio_helper;

struct Meeting : IMeetingService {
  SDKError SetEvent(IMeetingServiceEvent* ev) override {
    g_meeting_ev = ev;
    return SDKERR_SUCCESS;
  }
  SDKError Join(JoinParam& p) override {
    if (!p.param.withoutloginuserJoin.meetingNumber) return SDKERR_INVALID_PARAMETER;
    ++g_join_attempts;
    later(100, join_cb);
    return SDKERR_SUCCESS;
  }
  SDKError Leave(LeaveMeetingCmd) override {
    set_status(MEETING_STATUS_DISCONNECTING, 0);
    later(100, end_cb, GINT_TO_POINTER(EndMeetingReason_None));
    return SDKERR_SUCCESS;
  }
  MeetingStatus GetMeetingStatus() override { return g_status; }
  IMeetingInfo* GetMeetingInfo() override { return &g_info; }
  IMeetingParticipantsController* GetMeetingParticipantsController() override {
    return &g_participants;
  }
  IMeetingRecordingController* GetMeetingRecordingController() override { return &g_recording; }
  IMeetingChatController* GetMeetingChatController() override { return &g_chat; }
  IMeetingAudioController* GetMeetingAudioController() override { return &g_audio_ctrl; }
} g_meeting;

IAuthServiceEvent* g_auth_ev = nullptr;
gboolean auth_cb(gpointer) {
  if (g_auth_ev)
    g_auth_ev->onAuthenticationReturn(scenario() == "auth_fail" ? AUTHRET_JWTTOKENWRONG
                                                                : AUTHRET_SUCCESS);
  return G_SOURCE_REMOVE;
}
struct Auth : IAuthService {
  SDKError SetEvent(IAuthServiceEvent* ev) override {
    g_auth_ev = ev;
    return SDKERR_SUCCESS;
  }
  SDKError SDKAuth(AuthContext& ctx) override {
    if (!ctx.jwt_token || !*ctx.jwt_token) return SDKERR_INVALID_PARAMETER;
    later(50, auth_cb);
    return SDKERR_SUCCESS;
  }
} g_auth;

struct AudioSettings : IAudioSettingContext {
  SDKError EnableAutoJoinAudio(bool) override { return SDKERR_SUCCESS; }
} g_audio_settings;
struct VideoSettings : IVideoSettingContext {
  SDKError EnableAutoTurnOffVideoWhenJoinMeeting(bool) override { return SDKERR_SUCCESS; }
} g_video_settings;
struct Settings : ISettingService {
  IAudioSettingContext* GetAudioSettings() override { return &g_audio_settings; }
  IVideoSettingContext* GetVideoSettings() override { return &g_video_settings; }
} g_settings;

}  // namespace

extern "C" {
SDKError InitSDK(InitParam&) { return SDKERR_SUCCESS; }
SDKError CleanUPSDK() {
  if (g_audio_source) g_source_remove(g_audio_source);
  g_audio_source = 0;
  return SDKERR_SUCCESS;
}
SDKError CreateMeetingService(IMeetingService** out) {
  *out = &g_meeting;
  return SDKERR_SUCCESS;
}
SDKError DestroyMeetingService(IMeetingService*) { return SDKERR_SUCCESS; }
SDKError CreateAuthService(IAuthService** out) {
  *out = &g_auth;
  return SDKERR_SUCCESS;
}
SDKError DestroyAuthService(IAuthService*) { return SDKERR_SUCCESS; }
SDKError CreateSettingService(ISettingService** out) {
  *out = &g_settings;
  return SDKERR_SUCCESS;
}
SDKError DestroySettingService(ISettingService*) { return SDKERR_SUCCESS; }
IZoomSDKAudioRawDataHelper* GetAudioRawdataHelper() { return &g_audio_helper; }
}

}  // namespace ZOOMSDK
