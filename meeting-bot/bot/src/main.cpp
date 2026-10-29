// athena-meeting-bot: joins one Zoom meeting as "Athena" with the Zoom Meeting SDK for Linux,
// records one track per participant and leaves on its own (docs/zoom-meeting-bot-plan.md,
// lane 1A and the bot part of lane 3A). The launcher (server/athena/meetings.py)
// starts it per meeting; nothing of it runs between meetings.
//
// SDK calls are kept as close as possible to the Phase 0 spike's athena_spike.cpp, which
// compiled and ran against the real SDK 7.1.5 (removed; in git history at b96332dce842). Names the spike did not use are listed in
// meeting-bot/README.md ("SDK names beyond the spike").
//
// Threads: the GLib main loop (all SDK callbacks except raw audio arrive on it), the SDK's
// own threads, and athena::Core's one uploader thread. Nothing else.
//
// stdout: only `STATE <state> [<reason>]` lines, one per change, flushed. stderr: logs.
// Secrets (JWT, upload token, join token, passcode) are never printed.

#include <glib-unix.h>
#include <glib.h>
#include <signal.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include "athena/core.h"
#include "options.h"
#include "participants.h"
#include "rules.h"

#include "zoom_sdk.h"
#include "zoom_sdk_def.h"
#include "auth_service_interface.h"
#include "meeting_service_interface.h"
#include "setting_service_interface.h"
#include "meeting_service_components/meeting_audio_interface.h"
#include "meeting_service_components/meeting_chat_interface.h"
#include "meeting_service_components/meeting_participants_ctrl_interface.h"
#include "meeting_service_components/meeting_recording_interface.h"
#include "rawdata/rawdata_audio_helper_interface.h"
#include "rawdata/zoom_rawdata_api.h"
#include "zoom_sdk_raw_data_def.h"
// Breakout rooms: only to keep the alone rule from ending the meeting while everyone is in
// a breakout room, and to note it. Without this header the bot builds and runs without it.
#if __has_include("meeting_service_components/meeting_breakout_rooms_interface_v2.h")
#include "meeting_service_components/meeting_breakout_rooms_interface_v2.h"
#define ATHENA_HAVE_BREAKOUTS 1
#endif

USING_ZOOM_SDK_NAMESPACE

namespace {

using athena::bot::Breakouts;
using athena::bot::Decision;
using athena::bot::EndKind;
using athena::bot::JoinFail;
using athena::bot::Outcome;
using athena::bot::Status;

constexpr int kOwnerRetrySeconds = 3;       // Zoom suggests retrying every 1-5 s (spike)
constexpr int kPermissionRetrySeconds = 15; // CanStartRawRecording fallback (spike)
constexpr auto kSignalBudget = std::chrono::milliseconds(4000);   // finish() on SIGTERM
constexpr auto kNormalBudget = std::chrono::seconds(20);          // finish() after a meeting
constexpr auto kEarlyBudget = std::chrono::seconds(3);            // finish() if never joined
// Rejoin after a drop: tries not before 2 s, 20 s and 60 s after it; give up after 2 min.
constexpr int64_t kRejoinWindowMs = 120000;
constexpr int64_t kRejoinAtMs[athena::bot::kRejoinTries] = {2000, 20000, 60000};

athena::bot::Options g_opt;
std::unique_ptr<athena::bot::Rules> g_rules;
std::unique_ptr<athena::Core> g_core;
athena::bot::ParticipantTracker g_tracker;

GMainLoop* g_loop = nullptr;
IMeetingService* g_meeting = nullptr;
IAuthService* g_auth = nullptr;
ISettingService* g_settings = nullptr;
IZoomSDKAudioRawDataHelper* g_audio_helper = nullptr;

Status g_shown;                    // last status line printed
bool g_concluding = false;         // leaving: no more status lines until the final one
bool g_finished = false;
bool g_from_signal = false;
Decision g_final;
std::chrono::steady_clock::time_point g_deadline;
bool g_raw_started = false;       // raw audio subscribed in this stay in the meeting
bool g_had_raw = false;           // ... in any earlier one (the host allowed recording)
int g_asks_left = 1;              // recording requests left for this stay in the meeting
guint g_permission_timer = 0;     // the CanStartRawRecording retry, at most one
bool g_joined_once = false;
bool g_in_meeting = false;        // INMEETING and not left since (drop, waiting room)
std::atomic<bool> g_audio_on{false};
std::atomic<long long> g_last_audio_ms{0};

std::mutex g_names_mu;  // user id -> display name, read on the SDK's audio thread
std::map<unsigned int, std::string> g_names;
std::set<unsigned int> g_self_ids;  // every user id the bot had (it changes on a rejoin)

long long now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

void log(const std::string& s) {
  std::fprintf(stderr, "athena-meeting-bot: %s\n", s.c_str());
  std::fflush(stderr);
}

// One status line on stdout. Write errors (nobody reading after a server restart) are
// ignored: SIGPIPE is ignored and the bot keeps recording.
void emit(const Status& s) {
  if (s.state.empty() || s == g_shown) return;
  if (!s.reason.empty() && !athena::bot::valid_reason(s.reason)) {
    log("internal: invalid reason " + s.reason);
    return;
  }
  g_shown = s;
  std::string line = "STATE " + s.line() + "\n";
  std::fputs(line.c_str(), stdout);
  std::fflush(stdout);
  clearerr(stdout);
}

void show() {
  if (!g_concluding && g_rules) emit(g_rules->status());
}

std::string sdk_user_name(unsigned int user_id) {
  if (!g_meeting) return "";
  auto* pc = g_meeting->GetMeetingParticipantsController();
  if (!pc) return "";
  IUserInfo* u = pc->GetUserByUserID(user_id);
  if (!u || !u->GetUserName()) return "";
  return u->GetUserName();
}

unsigned int my_user_id() {
  auto* pc = g_meeting ? g_meeting->GetMeetingParticipantsController() : nullptr;
  IUserInfo* me = pc ? pc->GetMySelfUser() : nullptr;
  return me ? me->GetUserID() : 0;
}

bool is_self(unsigned int user_id) {
  std::lock_guard<std::mutex> lock(g_names_mu);
  return g_self_ids.count(user_id) != 0;
}

void conclude(const Decision& d, bool from_signal);

// ---- raw audio -----------------------------------------------------------------------

class AudioDelegate : public IZoomSDKAudioRawDataDelegate {
 public:
  void onMixedAudioRawDataReceived(AudioRawData*) override {}  // mixed audio: not used
  void onOneWayAudioRawDataReceived(AudioRawData* data, uint32_t user_id) override {
    if (!data || !g_audio_on || user_id == self_.load() || !g_core) return;
    const long long t = now_ms();
    g_last_audio_ms = t;
    const unsigned int rate = data->GetSampleRate();
    const unsigned int ch = data->GetChannelNum() ? data->GetChannelNum() : 1;
    const unsigned int len = data->GetBufferLen();
    if (!rate || !data->GetBuffer() || len < 2 * ch) return;
    const auto* pcm = reinterpret_cast<const int16_t*>(data->GetBuffer());
    size_t frames = len / 2 / ch;
    if (ch > 1) {  // the real SDK sends mono (spike); downmix just in case
      thread_local std::vector<int16_t> mono;
      mono.resize(frames);
      for (size_t i = 0; i < frames; ++i) {
        int sum = 0;
        for (unsigned int c = 0; c < ch; ++c) sum += pcm[i * ch + c];
        mono[i] = static_cast<int16_t>(sum / static_cast<int>(ch));
      }
      pcm = mono.data();
    }
    const long long start = t - static_cast<long long>(frames) * 1000 / rate;
    std::string name;
    if (!track_name(user_id, &name)) return;  // an earlier id of the bot (before a rejoin)
    g_core->on_audio(std::to_string(user_id), name, pcm, frames, static_cast<int>(rate),
                     start);
  }
  void onShareAudioRawDataReceived(AudioRawData*, uint32_t) override {}  // never share audio
  void onOneWayInterpreterAudioRawDataReceived(AudioRawData*, const zchar_t*) override {}
  std::atomic<unsigned int> self_{0};  // written on the loop, read on the audio thread

 private:
  // false: the user id was the bot's own (before a rejoin): never recorded.
  static bool track_name(unsigned int user_id, std::string* out) {
    {
      std::lock_guard<std::mutex> lock(g_names_mu);
      if (g_self_ids.count(user_id)) return false;
      auto it = g_names.find(user_id);
      if (it != g_names.end()) {
        *out = it->second;
        return true;
      }
    }
    std::string n = sdk_user_name(user_id);  // a new speaker before the next poll (as the spike)
    if (!n.empty()) {
      std::lock_guard<std::mutex> lock(g_names_mu);
      g_names.emplace(user_id, n);
    }
    *out = n;
    return true;
  }
};

AudioDelegate g_audio;

// ---- chat ----------------------------------------------------------------------------

class ChatEvents : public IMeetingChatCtrlEvent {
 public:
  void onChatMsgNotification(IChatMsgInfo* msg, const zchar_t* /*content*/) override {
    if (!msg || !g_core || g_concluding) return;
    const zchar_t* text = msg->GetContent();
    const zchar_t* from = msg->GetSenderDisplayName();
    long long at = static_cast<long long>(msg->GetTimeStamp()) * 1000;
    if (at <= 0) at = now_ms();
    // In a meeting the bot only receives chat to everyone or to itself (it is never host),
    // so anything not to everyone is private to Athena: Core drops it, never stored.
    const bool private_to_bot = !msg->IsChatToAll();
    g_core->on_chat(at, from ? from : "", std::to_string(msg->GetSenderUserId()),
                    text ? text : "", private_to_bot);
    log(private_to_bot ? "private chat message dropped" : "chat message kept");
  }
  void onChatStatusChangedNotification(ChatStatus*) override {}
  void onChatMsgDeleteNotification(const zchar_t*, SDKChatMessageDeleteType) override {}
  void onChatMessageEditNotification(IChatMsgInfo*) override {}
  void onShareMeetingChatStatusChanged(bool) override {}
  void onFileSendStart(ISDKFileSender*) override {}
  void onFileReceived(ISDKFileReceiver*) override {}
  void onFileTransferProgress(SDKFileTransferInfo*) override {}
};

ChatEvents g_chat;

// ---- recording permission: ask Zoom once, then wait (spike) --------------------------

bool try_start_raw_audio();
void stop_raw_audio();

class RecordingEvents : public IMeetingRecordingCtrlEvent {
 public:
  void onRecordingStatus(RecordingStatus) override {}
  void onCloudRecordingStatus(RecordingStatus) override {}
  void onRecordPrivilegeChanged(bool can_record) override {
    log(std::string("record privilege changed: ") + (can_record ? "yes" : "no"));
    if (can_record) try_start_raw_audio();
    else stop_raw_audio();  // the host took it away: Zoom stops the raw recording
  }
  void onLocalRecordingPrivilegeRequestStatus(RequestLocalRecordingStatus status) override {
    log("recording request answer: " + std::to_string(status));
    if (status == RequestLocalRecording_Granted) {
      try_start_raw_audio();
    } else if (status == RequestLocalRecording_Denied && !g_raw_started && g_rules) {
      g_rules->permission_denied();  // keep waiting: the host may still allow it later
      show();
    }  // Timeout: no answer; keep waiting, never ask again
  }
  void onRequestCloudRecordingResponse(RequestStartCloudRecordingStatus) override {}
  void onLocalRecordingPrivilegeRequested(IRequestLocalRecordingPrivilegeHandler*) override {}
  void onStartCloudRecordingRequested(IRequestStartCloudRecordingHandler*) override {}
  void onCloudRecordingStorageFull(time_t) override {}
  void onEnableAndStartSmartRecordingRequested(IRequestEnableAndStartSmartRecordingHandler*) override {}
  void onSmartRecordingEnableActionCallback(ISmartRecordingEnableActionHandler*) override {}
  void onTranscodingStatusChanged(TranscodingStatus, const zchar_t*) override {}
};

RecordingEvents g_recording_events;

bool try_start_raw_audio() {
  if (g_raw_started) return true;
  if (!g_meeting || g_concluding || !g_in_meeting) return false;
  auto* rec = g_meeting->GetMeetingRecordingController();
  if (!rec) return false;
  SDKError can = rec->CanStartRawRecording();
  if (can != SDKERR_SUCCESS) {
    static int logged = -1;  // log each new answer once, not on every retry
    if (logged != can) {
      logged = can;
      log("raw audio not allowed yet (CanStartRawRecording=" + std::to_string(can) + ")");
    }
    return false;
  }
  SDKError err = rec->StartRawRecording();
  if (err != SDKERR_SUCCESS) {
    log("StartRawRecording failed: " + std::to_string(err));
    // Back in after a drop the SDK may still hold the earlier raw recording: subscribe.
    if (!g_had_raw) return false;
  }
  g_audio_helper = GetAudioRawdataHelper();
  if (!g_audio_helper) {
    log("GetAudioRawdataHelper returned null");
    return false;
  }
  g_audio.self_ = my_user_id();
  g_audio_on = true;
  err = g_audio_helper->subscribe(&g_audio);
  if (err != SDKERR_SUCCESS) {
    g_audio_on = false;
    log("audio subscribe failed: " + std::to_string(err));
    return false;
  }
  g_raw_started = true;
  g_had_raw = true;
  log("raw audio subscribed");
  g_rules->recording(now_ms());
  show();
  return true;
}

gboolean permission_retry(gpointer) {
  if (g_concluding || try_start_raw_audio()) {
    g_permission_timer = 0;
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;  // also while out of the meeting: tries again once back in
}

// The host took the recording permission away while we recorded: Zoom stopped the raw
// recording, so stop taking audio, upload what we have and show waiting_permission. Once the
// host gives it back (onRecordPrivilegeChanged(true), or the 15 s retry) recording starts
// again; we never ask again, not even after a rejoin.
void stop_raw_audio() {
  if (!g_raw_started || g_concluding) return;
  log("recording permission taken away: raw audio stopped");
  g_audio_on = false;
  if (g_audio_helper) g_audio_helper->unSubscribe();
  g_raw_started = false;
  g_had_raw = false;
  if (g_core) g_core->close_chunks();
  g_rules->recording_stopped();
  show();  // waiting_permission
  if (!g_permission_timer)
    g_permission_timer = g_timeout_add_seconds(kPermissionRetrySeconds, permission_retry, nullptr);
}

// Ask the host at most once per stay in the meeting: once at the first join, and once
// more after a rejoin only if the host had allowed recording before (Zoom forgets it when
// the bot rejoins). A host who declined or never answered is not asked again.
void start_or_ask() {
  if (try_start_raw_audio()) return;
  auto* rec = g_meeting->GetMeetingRecordingController();
  if (g_asks_left > 0 && rec) {
    g_asks_left = 0;
    SDKError e = rec->RequestLocalRecordingPrivilege();
    log("recording request sent: " + std::to_string(e));
  }
  show();  // waiting_permission
  if (!g_permission_timer)
    g_permission_timer = g_timeout_add_seconds(kPermissionRetrySeconds, permission_retry, nullptr);
}

// The bot is out of the meeting for now (a drop, or the host moved it to the waiting
// room): stop taking audio and upload what was recorded. Core, its session id and the
// tracks' sequences stay; the same Core goes on when the bot is back in.
void left_meeting(const char* why) {
  if (!g_in_meeting) return;
  g_in_meeting = false;
  log(std::string("out of the meeting: ") + why);
  g_audio_on = false;
  if (g_raw_started && g_audio_helper) g_audio_helper->unSubscribe();
  g_raw_started = false;
  if (g_core) g_core->close_chunks();
}

// ---- participants and topic ----------------------------------------------------------

// Are breakout rooms open? The bot is never assigned to one and stays in the main room.
bool breakouts_open() {
#ifdef ATHENA_HAVE_BREAKOUTS
  auto* bo = g_meeting ? g_meeting->GetMeetingBOController() : nullptr;
  return bo && bo->IsBOStarted();
#else
  return false;
#endif
}

void poll_participants(long long t) {
  auto* pc = g_meeting ? g_meeting->GetMeetingParticipantsController() : nullptr;
  if (!pc) return;
  const unsigned int self = my_user_id();
  IList<unsigned int>* lst = pc->GetParticipantsList();
  if (!lst || !self) return;  // no answer this second: decide nothing
  std::map<unsigned int, std::string> snap;
  int others = 0;
  for (int i = 0; i < lst->GetCount(); ++i) {
    unsigned int id = lst->GetItem(i);
    if (id == self || is_self(id)) continue;  // also the bot's own entry before a rejoin
    ++others;
    IUserInfo* u = pc->GetUserByUserID(id);
    std::string n = u && u->GetUserName() ? u->GetUserName() : "";
    if (n.empty()) n = g_tracker.name_of(id);
    if (!n.empty()) snap.emplace(id, n);  // a name not known yet: next second
  }
  for (const auto& c : g_tracker.update(snap)) {
    if (g_core) g_core->on_participant(c.track_id, c.name, c.joined, t);
    if (c.joined) {
      std::lock_guard<std::mutex> lock(g_names_mu);
      g_names[static_cast<unsigned int>(std::stoul(c.track_id))] = c.name;
    }
  }
  switch (g_rules->others(others, t, breakouts_open())) {
    case Breakouts::MostLeft:
      log("breakout rooms are open and most participants left the main room; staying");
      if (g_core) g_core->on_breakouts(true, g_rules->breakouts_since());
      break;
    case Breakouts::Over:
      log("breakout rooms closed");
      if (g_core) g_core->on_breakouts(false, t);
      break;
    case Breakouts::None:
      break;
  }
}

void set_topic(const zchar_t* topic) {
  if (topic && *topic && g_core) g_core->set_topic(topic);
}

// ---- leaving -------------------------------------------------------------------------

// Core::finish blocks the main loop (up to 20 s after a meeting), so the GLib signal source
// can't run then. A SIGTERM/SIGINT during it gets these plain handlers instead: finish keeps
// going for at most kSignalBudget more, then the final line is written and the bot exits,
// within the launcher's 5 s rule. Only async-signal-safe calls in the handlers.
volatile sig_atomic_t g_term_in_finish = 0;
char g_final_line[96];
size_t g_final_len = 0;
int g_final_code = 0;

extern "C" void finish_term_handler(int) {
  if (!g_term_in_finish) {
    g_term_in_finish = 1;
    alarm(static_cast<unsigned>(
        std::chrono::duration_cast<std::chrono::seconds>(kSignalBudget).count()));
  }
}

extern "C" void finish_alarm_handler(int) {
  ssize_t w = write(STDOUT_FILENO, g_final_line, g_final_len);
  (void)w;
  _exit(g_final_code);
}

gboolean finish_cb(gpointer) {
  if (g_finished) return G_SOURCE_REMOVE;
  g_finished = true;
  const Status final_status{g_final.outcome == Outcome::Failed ? "failed" : "ended",
                            g_final.reason};
  std::snprintf(g_final_line, sizeof g_final_line, "STATE %s\n", final_status.line().c_str());
  g_final_len = std::strlen(g_final_line);
  g_final_code = g_final.outcome == Outcome::Failed ? 1 : 0;
  struct sigaction term {}, alrm {}, old_term {}, old_int {}, old_alrm {};
  term.sa_handler = finish_term_handler;
  sigemptyset(&term.sa_mask);
  alrm.sa_handler = finish_alarm_handler;
  sigemptyset(&alrm.sa_mask);
  sigaction(SIGALRM, &alrm, &old_alrm);
  sigaction(SIGTERM, &term, &old_term);
  sigaction(SIGINT, &term, &old_int);
  bool ok = g_core ? g_core->finish(g_deadline) : false;
  sigaction(SIGTERM, &old_term, nullptr);  // first, so no new alarm is armed after alarm(0)
  sigaction(SIGINT, &old_int, nullptr);
  alarm(0);
  sigaction(SIGALRM, &old_alrm, nullptr);
  if (g_term_in_finish) g_from_signal = true;  // exit at once, without the SDK teardown
  log(ok ? "session finished: everything sent" : "session finished: not everything was sent");
  emit(final_status);
  if (g_loop) g_main_loop_quit(g_loop);
  return G_SOURCE_REMOVE;
}

// Leave the meeting (if in one), give the SDK a moment to send the leave, then flush the
// last chunks and the chat, end the session, print the final line and quit the loop.
void conclude(const Decision& d, bool from_signal) {
  if (g_concluding || !d) return;
  g_concluding = true;
  g_final = d;
  g_from_signal = from_signal;
  std::chrono::steady_clock::duration budget = kEarlyBudget;
  if (from_signal) budget = kSignalBudget;
  else if (g_joined_once) budget = kNormalBudget;
  g_deadline = std::chrono::steady_clock::now() + budget;
  log("leaving: " + d.reason);
  g_audio_on = false;
  if (g_raw_started && g_audio_helper) g_audio_helper->unSubscribe();
  g_raw_started = false;
  MeetingStatus st = g_meeting ? g_meeting->GetMeetingStatus() : MEETING_STATUS_IDLE;
  if (st != MEETING_STATUS_IDLE && st != MEETING_STATUS_ENDED && st != MEETING_STATUS_FAILED) {
    g_meeting->Leave(LEAVE_MEETING);
    // finish once Zoom confirms (ENDED) or after this pause, whichever comes first
    g_timeout_add(from_signal ? 600 : 2000, finish_cb, nullptr);
  } else {
    g_idle_add(finish_cb, nullptr);
  }
}

void act(const Decision& d) {
  if (d) conclude(d, false);
  else show();
}

gboolean on_signal(gpointer) {
  if (!g_concluding) {
    conclude(g_rules->stop_signal(), true);
  } else if (!g_finished) {  // already leaving: keep the 5 s exit rule
    g_from_signal = true;
    auto cap = std::chrono::steady_clock::now() + kSignalBudget;
    if (cap < g_deadline) g_deadline = cap;
  }
  return G_SOURCE_CONTINUE;
}

void join();

gboolean tick(gpointer) {
  if (g_concluding) return G_SOURCE_REMOVE;
  const long long t = now_ms();
  if (g_rules->phase() == athena::bot::Phase::InMeeting) poll_participants(t);
  act(g_rules->tick(t, g_last_audio_ms.load(), g_core && g_core->stop_requested(),
                    g_core && g_core->upload_rejected()));
  if (!g_concluding && g_rules->rejoin_due(t)) {
    g_rules->rejoin_started(t);
    log("rejoining, try " + std::to_string(g_rules->rejoin_tries()) + " of " +
        std::to_string(athena::bot::kRejoinTries));
    join();
  }
  return g_concluding ? G_SOURCE_REMOVE : G_SOURCE_CONTINUE;
}

// ---- joining -------------------------------------------------------------------------

gboolean retry_join(gpointer) {
  if (!g_concluding) join();
  return G_SOURCE_REMOVE;
}

// In the meeting: the first time, after a rejoin or the SDK's own reconnect, or let in
// from the waiting room again. Each time: events set again, our (maybe new) user id, the
// topic, computer audio and raw audio, as a fresh join needs them.
void on_joined() {
  if (g_in_meeting) return;
  const bool back = g_joined_once;
  g_joined_once = true;
  g_in_meeting = true;
  g_rules->joined(now_ms());
  const unsigned int self = my_user_id();
  g_audio.self_ = self;
  if (self) {
    std::lock_guard<std::mutex> lock(g_names_mu);
    g_self_ids.insert(self);
    g_names.erase(self);
  }
  if (back) {
    log("back in the meeting");
    if (g_had_raw) g_asks_left = 1;  // Zoom forgets the permission when the bot rejoins
  }
  if (auto* chat = g_meeting->GetMeetingChatController()) chat->SetEvent(&g_chat);
  if (auto* rec = g_meeting->GetMeetingRecordingController()) rec->SetEvent(&g_recording_events);
  if (IMeetingInfo* info = g_meeting->GetMeetingInfo()) set_topic(info->GetMeetingTopic());
  // Raw audio stays empty unless we join computer audio (Zoom forum, since 6.3.5; spike).
  if (auto* audio = g_meeting->GetMeetingAudioController()) {
    SDKError e = audio->JoinVoip();
    log("JoinVoip: " + std::to_string(e));
  }
  poll_participants(now_ms());
  start_or_ask();
}

JoinFail map_fail(int result) {
  switch (result) {
    case MEETING_FAIL_PASSWORD_ERR: return JoinFail::WrongPasscode;
    case MEETING_FAIL_MEETING_NOT_START: return JoinFail::NotStarted;
    case MEETING_FAIL_MEETING_NOT_EXIST: return JoinFail::NotFound;
    case MEETING_FAIL_CANNOT_START_TOKENEXPIRE: return JoinFail::TokenExpired;
    case MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING: return JoinFail::OwnerNotIn;
    default: return JoinFail::Other;
  }
}

EndKind map_end(int result) {
  switch (result) {
    case EndMeetingReason_KickByHost: return EndKind::Removed;
    case EndMeetingReason_EndByHost: return EndKind::ByHost;
    case EndMeetingReason_JBHTimeOut: return EndKind::HostNeverCame;
    default: return EndKind::Other;
  }
}

class MeetingEvents : public IMeetingServiceEvent {
 public:
  void onMeetingStatusChanged(MeetingStatus status, int result) override {
    log("meeting status " + std::to_string(status) + " result " + std::to_string(result));
    if (g_concluding) {
      if (status == MEETING_STATUS_ENDED || status == MEETING_STATUS_FAILED)
        g_idle_add(finish_cb, nullptr);  // Zoom confirmed the leave
      return;
    }
    const long long t = now_ms();
    switch (status) {
      case MEETING_STATUS_WAITINGFORHOST:
        g_rules->waiting_for_host(t);
        show();
        break;
      case MEETING_STATUS_IN_WAITING_ROOM:
        left_meeting("moved to the waiting room");  // if the host moved us back there
        g_rules->waiting_room(t);
        show();
        break;
      case MEETING_STATUS_RECONNECTING:  // the SDK lost the connection, reconnects itself
        left_meeting("connection lost, the SDK reconnects");
        g_rules->reconnecting(t);
        show();
        break;
      case MEETING_STATUS_INMEETING:
        on_joined();
        break;
      case MEETING_STATUS_FAILED: {
        left_meeting("session failed");
        Decision d = g_rules->join_failed(map_fail(result), t);
        if (!d && g_rules->phase() == athena::bot::Phase::WaitingForOwner) {
          show();  // starting waiting_for_owner
          g_timeout_add_seconds(kOwnerRetrySeconds, retry_join, nullptr);
        }
        if (d) log("join failed, MeetingFailCode " + std::to_string(result));
        act(d);
        break;
      }
      case MEETING_STATUS_ENDED:
        // EndMeetingReason; with OBF a non-host end is taken as the owner leaving (spike);
        // while reconnecting, a non-host end is still a drop (rejoin)
        left_meeting("session ended");
        act(g_rules->meeting_ended(map_end(result)));
        break;
      default:
        break;
    }
  }
  void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
  void onMeetingParameterNotification(const MeetingParameter*) override {}
  void onSuspendParticipantsActivities() override {}
  void onAICompanionActiveChangeNotice(bool) override {}
  void onMeetingTopicChanged(const zchar_t* topic) override { set_topic(topic); }
  void onMeetingFullToWatchLiveStream(const zchar_t*) override {}
  void onUserNetworkStatusChanged(MeetingComponentType, ConnectionQuality, unsigned int, bool) override {}
};

MeetingEvents g_meeting_events;

void join() {
  if (g_concluding) return;  // e.g. SIGTERM handled in the same loop turn as the auth answer
  JoinParam jp;
  jp.userType = SDK_UT_WITHOUT_LOGIN;
  JoinParam4WithoutLogin& p = jp.param.withoutloginuserJoin;
  p.meetingNumber = std::stoull(g_opt.meeting_id);
  p.userName = g_opt.name.c_str();
  p.psw = g_opt.passcode.c_str();
  p.vanityID = nullptr;
  p.customer_key = nullptr;
  p.webinarToken = nullptr;
  p.isVideoOff = true;   // never send video
  p.isAudioOff = false;  // audio must be on to receive raw audio
  p.userZAK = g_opt.zak() ? g_opt.join_token.c_str() : nullptr;
  p.app_privilege_token = nullptr;
  p.onBehalfToken = g_opt.obf() ? g_opt.join_token.c_str() : nullptr;
  log(std::string("joining with ") + (g_opt.join_token.empty() ? "jwt only" : g_opt.join_token_kind));
  if (g_settings) {
    if (auto* a = g_settings->GetAudioSettings()) a->EnableAutoJoinAudio(true);
    if (auto* v = g_settings->GetVideoSettings()) v->EnableAutoTurnOffVideoWhenJoinMeeting(true);
  }
  SDKError err = g_meeting->Join(jp);
  if (err != SDKERR_SUCCESS) {
    log("Join call failed: " + std::to_string(err));
    // a rejoin try: counts as a failed try; the first join: failed join_failed
    act(g_rules->ever_joined() ? g_rules->join_failed(JoinFail::Other, now_ms())
                               : Decision::failed("join_failed"));
  }
}

class AuthEvents : public IAuthServiceEvent {
 public:
  void onAuthenticationReturn(AuthResult r) override {
    if (r == AUTHRET_SUCCESS) {
      log("SDK auth ok");
      join();
    } else {
      log("SDK auth failed: AuthResult " + std::to_string(r));
      act(Decision::failed("auth_failed"));
    }
  }
  void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
  void onLogout() override {}
  void onZoomIdentityExpired() override {}
  void onZoomAuthIdentityExpired() override {}
};

AuthEvents g_auth_events;

// Before the loop runs: print the failure, end the session quickly and exit non-zero.
int fail_early(const std::string& reason) {
  g_final = Decision::failed(reason);
  g_concluding = true;
  if (g_core) g_core->finish(std::chrono::steady_clock::now() + kEarlyBudget);
  emit(Status{"failed", reason});
  return 1;
}

}  // namespace

int main(int argc, char** argv) {
  signal(SIGPIPE, SIG_IGN);  // stdout may lose its reader; keep recording

  std::vector<std::string> args(argv + 1, argv + argc);
  auto parsed = athena::bot::parse_options(args, [](const char* n) { return std::getenv(n); });
  if (!parsed.options) {
    log(parsed.error);
    std::fputs(athena::bot::usage(), stderr);
    emit(Status{"failed", "bad_arguments"});
    return 2;
  }
  g_opt = *parsed.options;

  std::error_code ec;
  std::filesystem::create_directories(g_opt.work_dir, ec);
  if (ec) {
    log("cannot create the work dir: " + ec.message());
    emit(Status{"failed", "work_dir"});
    return 1;
  }

  const long long start = now_ms();
  athena::bot::RulesConfig rc;
  rc.max_ms = g_opt.max_seconds * 1000LL;
  rc.alone_ms = g_opt.alone_seconds * 1000LL;
  rc.wait_ms = g_opt.waiting_room_seconds * 1000LL;
  rc.obf = g_opt.obf();
  rc.rejoin_window_ms = kRejoinWindowMs;
  for (int i = 0; i < athena::bot::kRejoinTries; ++i) rc.rejoin_at_ms[i] = kRejoinAtMs[i];
#ifdef ATHENA_ZOOM_SDK_STUB
  // Sandbox stub builds only (never the real SDK): the scenario tests shrink the rejoin
  // times tenfold, so a 2-minute give-up runs in 12 s.
  if (const char* f = std::getenv("ATHENA_SDKSTUB_FAST_REJOIN"); f && *f == '1') {
    rc.rejoin_window_ms /= 10;
    for (auto& at : rc.rejoin_at_ms) at /= 10;
  }
#endif
  g_rules = std::make_unique<athena::bot::Rules>(rc, start);

  athena::CoreConfig cc;
  cc.upload_url = g_opt.upload_url;
  cc.session_id = g_opt.session_id;
  cc.upload_token = g_opt.upload_token;
  cc.codec = g_opt.codec;
  cc.work_dir = g_opt.work_dir;
  cc.max_session_seconds = g_opt.max_seconds;
  g_core = std::make_unique<athena::Core>(cc);

  InitParam init;
  init.strWebDomain = "https://zoom.us";
  init.strSupportUrl = "https://zoom.us";
  init.emLanguageID = LANGUAGE_English;
  init.enableLogByDefault = g_opt.sdk_log;  // ~/.zoomsdk/logs, only for manual debugging
  init.enableGenerateDump = false;
  if (InitSDK(init) != SDKERR_SUCCESS) return fail_early("sdk_error");
  if (CreateMeetingService(&g_meeting) != SDKERR_SUCCESS || !g_meeting ||
      CreateSettingService(&g_settings) != SDKERR_SUCCESS ||
      CreateAuthService(&g_auth) != SDKERR_SUCCESS || !g_auth) {
    int rc_fail = fail_early("sdk_error");
    CleanUPSDK();
    return rc_fail;
  }
  g_meeting->SetEvent(&g_meeting_events);
  g_auth->SetEvent(&g_auth_events);

  g_loop = g_main_loop_new(nullptr, FALSE);
  g_unix_signal_add(SIGINT, on_signal, nullptr);
  g_unix_signal_add(SIGTERM, on_signal, nullptr);
  g_timeout_add_seconds(1, tick, nullptr);

  AuthContext ctx;
  ctx.jwt_token = g_opt.jwt.c_str();
  if (g_auth->SDKAuth(ctx) != SDKERR_SUCCESS) {
    log("SDKAuth call failed");
    act(Decision::failed("auth_failed"));
  }
  g_main_loop_run(g_loop);  // all SDK callbacks (but raw audio) arrive on this loop

  const bool failed = g_final.outcome == Outcome::Failed;
  if (g_from_signal) {
    // SIGTERM: the launcher wants us gone within 5 s. Everything is flushed and the final
    // line printed; skip the SDK teardown, whose duration we don't control.
    std::fflush(stdout);
    std::fflush(stderr);
    _exit(failed ? 1 : 0);
  }
  if (g_raw_started && g_audio_helper) g_audio_helper->unSubscribe();
  g_core.reset();
  DestroyAuthService(g_auth);
  DestroySettingService(g_settings);
  DestroyMeetingService(g_meeting);
  CleanUPSDK();
  g_main_loop_unref(g_loop);
  return failed ? 1 : 0;
}
