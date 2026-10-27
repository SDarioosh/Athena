// Athena Zoom bot: Phase 0 spike (throwaway, never merged into the real bot as is).
//
// UNVERIFIED: written in a sandbox without the Zoom Meeting SDK, against the Linux SDK
// API as we know it (6.x: IMeetingService, IAuthService::SDKAuth,
// IZoomSDKAudioRawDataDelegate::onOneWayAudioRawDataReceived,
// IMeetingChatCtrlEvent::onChatMsgNotification). The event interfaces gain pure virtual
// methods between SDK releases: if the compiler says a class is abstract, add an empty
// override for each method it lists (copy the signature from the SDK header). Names that
// differ in your SDK version are yours to fix; note them in docs/meeting-bot-spike.md.
//
// What it does: joins one meeting as "Athena" with video off and never subscribes to
// video or screen share; records whatever per-participant raw audio Zoom delivers, one
// PCM file per Zoom user id plus participants.json (user id -> display name); logs chat
// messages to chat.jsonl; leaves after --minutes, when the meeting ends, when removed, or
// on SIGINT/SIGTERM. It never asks for recording permission, posts nothing in chat and
// takes no commands from participants (owner's rule). If Zoom delivers no raw audio it
// stays in the meeting and says `STATE recording no_audio`. Meetings in other accounts:
// ATHENA_ZOOM_JOIN_TOKEN holds Athena's own account's ZAK (ATHENA_ZOOM_JOIN_TOKEN_KIND=zak,
// default) or an on-behalf-of token from the owner (kind obf, only while he is there).
//
// Status lines on stdout, one per change (measure.sh and later the launcher read them):
//   STATE starting | waiting_room | recording | recording no_audio | ended <reason> |
//         failed <reason>
// Other output is prefixed `LOG`.

#include <glib.h>
#include <glib-unix.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>

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

USING_ZOOM_SDK_NAMESPACE

namespace {

struct Options {
    std::string meeting_id;
    std::string passcode;
    std::string name = "Athena";
    std::string out_dir = "out";
    std::string jwt;              // env ATHENA_SPIKE_JWT (from sign_jwt.py)
    // env ATHENA_ZOOM_JOIN_TOKEN (the name lane 1B's launcher passes) with
    // ATHENA_ZOOM_JOIN_TOKEN_KIND: "zak" (default) = ZAK of Athena's own Zoom account, the
    // primary path for other accounts' meetings, no one else needs to be there; "obf" =
    // on-behalf-of token from the owner's account, fallback, works only while the owner is
    // in the meeting. Empty: JWT only (meetings in the app owner's own account).
    std::string join_token;
    std::string join_token_kind = "zak";
    std::string recording_token;  // env ATHENA_SPIKE_RECORDING_TOKEN: host-issued local-recording
                                  // join token, only if the host chose to give one
    int minutes = 30;
    int no_audio_seconds = 60;
};

Options g_opt;
GMainLoop* g_loop = nullptr;
IMeetingService* g_meeting = nullptr;
IAuthService* g_auth = nullptr;
ISettingService* g_settings = nullptr;
IZoomSDKAudioRawDataHelper* g_audio_helper = nullptr;
std::string g_state;
std::string g_end_reason = "left";
std::atomic<bool> g_leaving{false};
std::atomic<bool> g_failed{false};
std::atomic<long long> g_last_audio_ms{0};
bool g_raw_started = false;
long long g_joined_ms = 0;

long long now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

std::string iso_utc(time_t t) {
    char buf[32];
    struct tm tm_utc;
    gmtime_r(&t, &tm_utc);
    strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
    return buf;
}

std::string json_str(const std::string& s) {
    std::ostringstream o;
    o << '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\n': o << "\\n"; break;
            case '\r': o << "\\r"; break;
            case '\t': o << "\\t"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    snprintf(b, sizeof b, "\\u%04x", c);
                    o << b;
                } else {
                    o << c;
                }
        }
    }
    o << '"';
    return o.str();
}

void state(const std::string& s) {
    if (s == g_state) return;
    g_state = s;
    std::cout << "STATE " << s << std::endl;  // endl flushes: the launcher reads it live
}

void log(const std::string& s) { std::cout << "LOG " << s << std::endl; }

std::string user_name(unsigned int user_id) {
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

// --- per-participant tracks -------------------------------------------------------------

struct Track {
    std::ofstream file;
    std::string name;
    unsigned long long bytes = 0;
    long long first_ms = 0;
};

class Tracks {
public:
    void write(unsigned int user_id, const char* buf, unsigned int len, unsigned int rate,
               unsigned int channels) {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = tracks_.find(user_id);
        bool fresh = it == tracks_.end();
        if (fresh) {
            auto t = std::make_unique<Track>();
            std::string path = g_opt.out_dir + "/track-" + std::to_string(user_id) + ".pcm";
            t->file.open(path, std::ios::binary | std::ios::app);
            t->name = user_name(user_id);
            t->first_ms = now_ms();
            it = tracks_.emplace(user_id, std::move(t)).first;
            rate_ = rate;
            channels_ = channels;
            log("new track " + std::to_string(user_id) + " " + json_str(it->second->name) +
                " " + std::to_string(rate) + " Hz " + std::to_string(channels) + " ch");
        }
        it->second->file.write(buf, len);
        it->second->bytes += len;
        if (fresh) write_sidecar_locked();
    }

    void close() {
        std::lock_guard<std::mutex> lock(mu_);
        for (auto& [id, t] : tracks_) {
            std::string n = user_name(id);  // pick up renames; keep the old name if they left
            if (!n.empty()) t->name = n;
            t->file.close();
        }
        write_sidecar_locked();
    }

private:
    void write_sidecar_locked() {
        std::string tmp = g_opt.out_dir + "/participants.json.tmp";
        std::ofstream o(tmp, std::ios::trunc);
        o << "{\n  \"format\": \"s16le\",\n  \"sample_rate\": " << rate_
          << ",\n  \"channels\": " << channels_ << ",\n  \"tracks\": {";
        bool first = true;
        for (auto& [id, t] : tracks_) {
            o << (first ? "\n" : ",\n") << "    " << json_str(std::to_string(id))
              << ": {\"name\": " << json_str(t->name) << ", \"file\": "
              << json_str("track-" + std::to_string(id) + ".pcm") << ", \"bytes\": " << t->bytes
              << ", \"first_audio\": " << json_str(iso_utc(t->first_ms / 1000)) << "}";
            first = false;
        }
        o << "\n  }\n}\n";
        o.close();
        std::rename(tmp.c_str(), (g_opt.out_dir + "/participants.json").c_str());
    }

    std::mutex mu_;
    std::map<unsigned int, std::unique_ptr<Track>> tracks_;
    unsigned int rate_ = 0;
    unsigned int channels_ = 1;
};

Tracks g_tracks;

class AudioDelegate : public IZoomSDKAudioRawDataDelegate {
public:
    void onMixedAudioRawDataReceived(AudioRawData*) override {}  // mixed audio: not used
    void onOneWayAudioRawDataReceived(AudioRawData* data, uint32_t user_id) override {
        if (!data || user_id == self_) return;
        g_last_audio_ms = now_ms();
        g_tracks.write(user_id, data->GetBuffer(), data->GetBufferLen(), data->GetSampleRate(),
                       data->GetChannelNum());
    }
    void onShareAudioRawDataReceived(AudioRawData*, uint32_t) override {}  // never share audio
    void onOneWayInterpreterAudioRawDataReceived(AudioRawData*, const zchar_t*) override {}
    unsigned int self_ = 0;
};

AudioDelegate g_audio;

// --- chat ---------------------------------------------------------------------------------

class ChatEvents : public IMeetingChatCtrlEvent {
public:
    explicit ChatEvents(const std::string& path) : out_(path, std::ios::app) {}
    void onChatMsgNotification(IChatMsgInfo* msg, const zchar_t* /*content*/) override {
        if (!msg) return;
        const zchar_t* text = msg->GetContent();
        const zchar_t* from = msg->GetSenderDisplayName();
        out_ << "{\"at\": " << json_str(iso_utc(msg->GetTimeStamp()))
             << ", \"from\": " << json_str(from ? from : "")
             << ", \"zoom_user_id\": " << json_str(std::to_string(msg->GetSenderUserId()))
             << ", \"text\": " << json_str(text ? text : "")
             << ", \"to_all\": " << (msg->IsChatToAll() ? "true" : "false") << "}\n";
        out_.flush();
        log("chat message logged");
    }
    void onChatStatusChangedNotification(ChatStatus*) override {}
    void onChatMsgDeleteNotification(const zchar_t*, SDKChatMessageDeleteType) override {}
    void onChatMessageEditNotification(IChatMsgInfo*) override {}
    void onShareMeetingChatStatusChanged(bool) override {}
    void onFileSendStart(ISDKFileSender*) override {}
    void onFileReceived(ISDKFileReceiver*) override {}
    void onFileTransferProgress(SDKFileTransferInfo*) override {}

private:
    std::ofstream out_;
};

std::unique_ptr<ChatEvents> g_chat;

// --- raw audio: only what Zoom allows ---------------------------------------------------

void try_start_raw_audio();

// Listens only. The bot never calls RequestLocalRecordingPrivilege (owner's rule): if the
// host or Zoom grants recording on their own, this starts the audio.
class RecordingEvents : public IMeetingRecordingCtrlEvent {
public:
    void onRecordingStatus(RecordingStatus) override {}
    void onCloudRecordingStatus(RecordingStatus) override {}
    void onRecordPrivilegeChanged(bool can_record) override {
        log(std::string("record privilege changed: ") + (can_record ? "yes" : "no"));
        if (can_record) try_start_raw_audio();
    }
    void onLocalRecordingPrivilegeRequestStatus(RequestLocalRecordingStatus) override {}
    void onRequestCloudRecordingResponse(RequestStartCloudRecordingStatus) override {}
    void onLocalRecordingPrivilegeRequested(IRequestLocalRecordingPrivilegeHandler*) override {}
    void onStartCloudRecordingRequested(IRequestStartCloudRecordingHandler*) override {}
    void onCloudRecordingStorageFull(time_t) override {}
    void onEnableAndStartSmartRecordingRequested(IRequestEnableAndStartSmartRecordingHandler*) override {}
    void onSmartRecordingEnableActionCallback(ISmartRecordingEnableActionHandler*) override {}
    void onTranscodingStatusChanged(TranscodingStatus, const zchar_t*) override {}
};

RecordingEvents g_recording_events;

void try_start_raw_audio() {
    if (g_raw_started || !g_meeting) return;
    auto* rec = g_meeting->GetMeetingRecordingController();
    if (!rec) return;
    SDKError can = rec->CanStartRawRecording();
    if (can != SDKERR_SUCCESS) {
        static int logged = -1;  // log each new answer once, not every 5 s
        if (logged != can) {
            logged = can;
            log("raw audio not allowed by Zoom (CanStartRawRecording=" + std::to_string(can) +
                "); staying in the meeting without asking");
        }
        return;
    }
    SDKError err = rec->StartRawRecording();
    if (err != SDKERR_SUCCESS) {
        log("StartRawRecording failed: " + std::to_string(err));
        return;
    }
    g_audio_helper = GetAudioRawdataHelper();
    if (!g_audio_helper) {
        log("GetAudioRawdataHelper returned null");
        return;
    }
    g_audio.self_ = my_user_id();
    err = g_audio_helper->subscribe(&g_audio);
    if (err != SDKERR_SUCCESS) {
        log("audio subscribe failed: " + std::to_string(err));
        return;
    }
    g_raw_started = true;
    log("raw audio subscribed");
}

// --- leaving ------------------------------------------------------------------------------

gboolean quit_loop(gpointer) {
    if (g_loop) g_main_loop_quit(g_loop);
    return G_SOURCE_REMOVE;
}

void leave(const std::string& reason) {
    if (g_leaving.exchange(true)) return;
    g_end_reason = reason;
    log("leaving: " + reason);
    if (g_meeting && g_meeting->GetMeetingStatus() != MEETING_STATUS_IDLE) {
        g_meeting->Leave(LEAVE_MEETING);
        g_timeout_add_seconds(10, quit_loop, nullptr);  // in case ENDED never arrives
    } else {
        quit_loop(nullptr);
    }
}

void fail(const std::string& reason) {
    g_failed = true;
    state("failed " + reason);
    leave(reason);
}

gboolean on_duration_over(gpointer) {
    leave("duration");
    return G_SOURCE_REMOVE;
}

gboolean on_signal(gpointer) {
    leave("signal");
    return G_SOURCE_REMOVE;
}

// Checks every 5 s, only while in the meeting, whether Zoom is delivering audio.
gboolean audio_watch(gpointer) {
    if (g_leaving) return G_SOURCE_REMOVE;
    long long last = g_last_audio_ms.load();
    long long quiet = now_ms() - (last ? last : g_joined_ms);
    if (quiet >= g_opt.no_audio_seconds * 1000LL) {
        state("recording no_audio");
        try_start_raw_audio();  // Zoom may have allowed it meanwhile (no request is sent)
    } else if (last) {
        state("recording");
    }
    return G_SOURCE_CONTINUE;
}

void on_joined() {
    state("recording");
    if (g_joined_ms) return;  // back in after a reconnect: timers and events already set
    g_joined_ms = now_ms();
    auto* chat = g_meeting->GetMeetingChatController();
    if (chat) {
        g_chat = std::make_unique<ChatEvents>(g_opt.out_dir + "/chat.jsonl");
        chat->SetEvent(g_chat.get());
    }
    if (auto* rec = g_meeting->GetMeetingRecordingController()) rec->SetEvent(&g_recording_events);
    // Raw audio stays empty unless we join computer audio (seen in Zoom's forum since 6.3.5).
    if (auto* audio = g_meeting->GetMeetingAudioController()) {
        SDKError e = audio->JoinVoip();
        log("JoinVoip: " + std::to_string(e));
    }
    try_start_raw_audio();
    g_timeout_add_seconds(5, audio_watch, nullptr);
    g_timeout_add_seconds(static_cast<guint>(g_opt.minutes) * 60, on_duration_over, nullptr);
}

class MeetingEvents : public IMeetingServiceEvent {
public:
    void onMeetingStatusChanged(MeetingStatus status, int result) override {
        log("meeting status " + std::to_string(status) + " result " + std::to_string(result));
        switch (status) {
            case MEETING_STATUS_IN_WAITING_ROOM: state("waiting_room"); break;
            case MEETING_STATUS_INMEETING: on_joined(); break;
            case MEETING_STATUS_FAILED:
                // result is a MeetingFailCode, e.g. MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING
                // when an OBF token's user is not in the meeting (SDK >= 6.6.10), or an
                // auth error when Zoom refuses the ZAK (what test C finds out).
                fail("join_failed_" + std::to_string(result));
                break;
            case MEETING_STATUS_ENDED:
                // result is a MeetingEndReason (removed by host, host ended, ...).
                if (!g_leaving) g_end_reason = "meeting_ended_" + std::to_string(result);
                g_leaving = true;
                quit_loop(nullptr);
                break;
            default: break;
        }
    }
    void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
    void onMeetingParameterNotification(const MeetingParameter*) override {}
    void onSuspendParticipantsActivities() override {}
    void onAICompanionActiveChangeNotice(bool) override {}
    void onMeetingTopicChanged(const zchar_t*) override {}
    void onMeetingFullToWatchLiveStream(const zchar_t*) override {}
    void onUserNetworkStatusChanged(MeetingComponentType, ConnectionQuality, unsigned int, bool) override {}
};

MeetingEvents g_meeting_events;

void join() {
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
    bool has_token = !g_opt.join_token.empty();
    p.userZAK = has_token && g_opt.join_token_kind == "zak" ? g_opt.join_token.c_str() : nullptr;
    p.app_privilege_token = g_opt.recording_token.empty() ? nullptr : g_opt.recording_token.c_str();
    p.onBehalfToken = has_token && g_opt.join_token_kind == "obf" ? g_opt.join_token.c_str() : nullptr;
    log(std::string("joining with ") + (has_token ? g_opt.join_token_kind : "jwt only"));
    // Newer SDKs also have p.eAudioRawdataSamplingRate (32 kHz default); left as is. The
    // real rate is written to participants.json from each packet.
    if (g_settings) {
        if (auto* a = g_settings->GetAudioSettings()) a->EnableAutoJoinAudio(true);
        if (auto* v = g_settings->GetVideoSettings()) v->EnableAutoTurnOffVideoWhenJoinMeeting(true);
    }
    SDKError err = g_meeting->Join(jp);
    if (err != SDKERR_SUCCESS) fail("join_call_" + std::to_string(err));
}

class AuthEvents : public IAuthServiceEvent {
public:
    void onAuthenticationReturn(AuthResult r) override {
        if (r == AUTHRET_SUCCESS) {
            log("SDK auth ok");
            join();
        } else {
            fail("sdk_auth_" + std::to_string(r));
        }
    }
    void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
    void onLogout() override {}
    void onZoomIdentityExpired() override {}
    void onZoomAuthIdentityExpired() override {}
};

AuthEvents g_auth_events;

const char* env_or(const char* name, const char* def) {
    const char* v = std::getenv(name);
    return v ? v : def;
}

void usage() {
    std::cerr << "usage: athena-meeting-spike --meeting-id ID --passcode PW [--name Athena]\n"
                 "         [--minutes 30] [--out DIR] [--no-audio-seconds 60]\n"
                 "env: ATHENA_SPIKE_JWT (required, from sign_jwt.py)\n"
                 "     ATHENA_ZOOM_JOIN_TOKEN + ATHENA_ZOOM_JOIN_TOKEN_KIND=zak|obf (default zak)\n"
                 "     ATHENA_SPIKE_RECORDING_TOKEN (host-issued, optional)\n";
}

bool parse(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> std::string { return i + 1 < argc ? argv[++i] : ""; };
        if (a == "--meeting-id") g_opt.meeting_id = next();
        else if (a == "--passcode") g_opt.passcode = next();
        else if (a == "--name") g_opt.name = next();
        else if (a == "--out") g_opt.out_dir = next();
        else if (a == "--minutes") g_opt.minutes = std::atoi(next().c_str());
        else if (a == "--no-audio-seconds") g_opt.no_audio_seconds = std::atoi(next().c_str());
        else return false;
    }
    g_opt.jwt = env_or("ATHENA_SPIKE_JWT", "");
    g_opt.join_token = env_or("ATHENA_ZOOM_JOIN_TOKEN", "");
    g_opt.join_token_kind = env_or("ATHENA_ZOOM_JOIN_TOKEN_KIND", "zak");
    if (g_opt.join_token_kind != "zak" && g_opt.join_token_kind != "obf") return false;
    g_opt.recording_token = env_or("ATHENA_SPIKE_RECORDING_TOKEN", "");
    return !g_opt.meeting_id.empty() && !g_opt.jwt.empty() && g_opt.minutes > 0 &&
           g_opt.no_audio_seconds > 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (!parse(argc, argv)) {
        usage();
        return 2;
    }
    std::string mk = "mkdir -p '" + g_opt.out_dir + "'";
    if (std::system(mk.c_str()) != 0) return 2;
    state("starting");

    InitParam init;
    init.strWebDomain = "https://zoom.us";
    init.strSupportUrl = "https://zoom.us";
    init.emLanguageID = LANGUAGE_English;
    init.enableLogByDefault = true;     // SDK logs go to ~/.zoomsdk/logs (measure.sh lists them)
    init.enableGenerateDump = false;
    if (InitSDK(init) != SDKERR_SUCCESS) {
        state("failed sdk_init");
        return 1;
    }
    if (CreateMeetingService(&g_meeting) != SDKERR_SUCCESS || !g_meeting ||
        CreateSettingService(&g_settings) != SDKERR_SUCCESS ||
        CreateAuthService(&g_auth) != SDKERR_SUCCESS || !g_auth) {
        state("failed sdk_services");
        CleanUPSDK();
        return 1;
    }
    g_meeting->SetEvent(&g_meeting_events);
    g_auth->SetEvent(&g_auth_events);

    g_loop = g_main_loop_new(nullptr, FALSE);
    g_unix_signal_add(SIGINT, on_signal, nullptr);
    g_unix_signal_add(SIGTERM, on_signal, nullptr);

    AuthContext ctx;
    ctx.jwt_token = g_opt.jwt.c_str();
    if (g_auth->SDKAuth(ctx) != SDKERR_SUCCESS) {
        state("failed sdk_auth_call");
    } else {
        g_main_loop_run(g_loop);  // all SDK callbacks arrive on this loop
    }

    if (g_audio_helper) g_audio_helper->unSubscribe();
    g_tracks.close();
    g_chat.reset();
    DestroyAuthService(g_auth);
    DestroySettingService(g_settings);
    DestroyMeetingService(g_meeting);
    CleanUPSDK();
    g_main_loop_unref(g_loop);
    if (!g_failed) state("ended " + g_end_reason);
    return g_failed ? 1 : 0;
}
