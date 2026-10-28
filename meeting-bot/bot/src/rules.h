// The bot's join, wait and leave rules as a small state machine with no Zoom SDK in it:
// main.cpp maps SDK callbacks to these events and prints status() whenever it changes.
// Times are wall-clock epoch milliseconds; the host tests drive it with made-up times.
#pragma once

#include <cstdint>
#include <string>

namespace athena::bot {

struct RulesConfig {
  int64_t max_ms = 0;       // --max-seconds, counted from the bot's start
  int64_t alone_ms = 0;     // --alone-seconds: only participant this long -> ended alone
  int64_t wait_ms = 0;      // --waiting-room-seconds: waiting room, host, owner (obf)
  int64_t no_audio_ms = 60000;  // recording but no frames this long -> recording no_audio
  bool obf = false;         // joined with an on-behalf-of token
  // Rejoin after a drop: try k (0-based) not before drop + rejoin_at_ms[k], nor while
  // the SDK reconnects on its own or a try is still out; give up kRejoinTries failed tries
  // or rejoin_window_ms after the drop, whichever comes first (ended connection_lost).
  int64_t rejoin_window_ms = 120000;
  int64_t rejoin_at_ms[3] = {2000, 20000, 60000};
};

constexpr int kRejoinTries = 3;

// SDK-neutral join failure (main.cpp maps MeetingFailCode to it). The last four are Zoom
// refusing the bot itself, not the meeting: another try can't change them.
enum class JoinFail {
  WrongPasscode, NotStarted, NotFound, TokenExpired, OwnerNotIn,
  ExternalNotAllowed,  // the Marketplace app isn't published: only its own account's meetings
  OutsideUsersBlocked,  // the host lets in only users of certain accounts or domains
  BlockedByAdmin,       // an account admin blocks the app
  SignInRequired,       // the meeting needs a signed-in join token (OBF or ZAK)
  Other
};
// SDK-neutral end of the meeting session (main.cpp maps EndMeetingReason to it).
enum class EndKind { ByHost, Removed, HostNeverCame, Other };

enum class Outcome { None, Ended, Failed };

struct Decision {
  Outcome outcome = Outcome::None;
  std::string reason;
  explicit operator bool() const { return outcome != Outcome::None; }
  static Decision ended(std::string r) { return {Outcome::Ended, std::move(r)}; }
  static Decision failed(std::string r) { return {Outcome::Failed, std::move(r)}; }
};

struct Status {
  std::string state;   // "" = nothing to print yet (the launcher already shows starting)
  std::string reason;  // may be empty
  std::string line() const { return reason.empty() ? state : state + " " + reason; }
  bool operator==(const Status& o) const { return state == o.state && reason == o.reason; }
  bool operator!=(const Status& o) const { return !(*this == o); }
};

// Reconnecting: dropped after joining; the SDK reconnects on its own or the bot rejoins.
enum class Phase {
  Joining, WaitingForOwner, WaitingForHost, WaitingRoom, InMeeting, Reconnecting, Done
};

// What a participant poll changed about breakout rooms (main.cpp notes it in the session).
enum class Breakouts {
  None,
  MostLeft,  // breakout rooms are open and most participants left the main room
  Over,      // breakout rooms closed after MostLeft
};

class Rules {
 public:
  Rules(RulesConfig cfg, int64_t start_ms);

  // Zoom put us in the waiting room / waiting for the host to start the meeting. After
  // joining, the waiting room means the host moved Athena back there.
  void waiting_room(int64_t now);
  void waiting_for_host(int64_t now);
  // Fully in the meeting: first time, after a rejoin or let in from the waiting room again
  // (max duration counts from the start; recording must be started again).
  void joined(int64_t now);
  // The SDK lost the connection and reconnects on its own (MEETING_STATUS_RECONNECTING).
  void reconnecting(int64_t now);
  // Participants other than the bot, from the poll of the participant list, and whether
  // breakout rooms are open. While they are, the alone rule waits.
  Breakouts others(int count, int64_t now, bool breakouts_open = false);
  // Raw audio subscribed: recording.
  void recording(int64_t now);
  // The host took the recording permission away: raw audio stopped (waiting_permission
  // until it is given back; the bot never asks again).
  void recording_stopped();
  // The host declined the recording request.
  void permission_denied();

  // A join attempt (or the session after joining) failed. None = go on: retry the join
  // (obf, owner not in yet, within the wait: main.cpp tries again in 3 s) or, after a
  // drop, rejoin when rejoin_due() says so.
  Decision join_failed(JoinFail why, int64_t now);
  // Zoom ended our meeting session (not because we left). None = a drop: rejoin.
  Decision meeting_ended(EndKind why);
  // SIGTERM / SIGINT.
  Decision stop_signal();
  // Every second while the bot runs. last_audio_ms = the newest raw audio frame (0: none).
  Decision tick(int64_t now, int64_t last_audio_ms, bool server_stop, bool upload_rejected);

  // After a drop: call join() now (then rejoin_started()).
  bool rejoin_due(int64_t now) const;
  void rejoin_started(int64_t now);
  int rejoin_tries() const { return tries_; }

  // What the status line should say now.
  Status status() const;
  Phase phase() const { return phase_; }
  bool done() const { return phase_ == Phase::Done; }
  bool ever_joined() const { return joined_ms_ != 0; }
  bool is_recording() const { return recording_; }
  bool ever_recorded() const { return recording_ms_ != 0; }
  int64_t breakouts_since() const { return bo_since_; }

 private:
  Decision conclude(Decision d);
  void left_meeting();               // recording and the alone timer stop
  void start_drop(int64_t now, bool by_sdk);

  RulesConfig cfg_;
  int64_t start_ms_;
  Phase phase_ = Phase::Joining;
  int64_t phase_since_ = 0;      // waiting room / host wait start
  int64_t owner_wait_since_ = 0; // first refusal because the owner wasn't in
  int64_t joined_ms_ = 0;
  int64_t alone_since_ = 0;      // 0 = someone else is here (or not known yet)
  bool others_known_ = false;
  bool recording_ = false;
  int64_t recording_ms_ = 0;     // the latest start of recording
  bool denied_ = false;
  bool no_audio_ = false;
  // drop and rejoin
  int64_t drop_since_ = 0;
  bool sdk_reconnecting_ = false;
  bool attempt_ = false;         // a rejoin try is out (Join called, no answer yet)
  int tries_ = 0;
  // breakout rooms
  bool bo_open_ = false;
  bool bo_noted_ = false;
  int64_t bo_since_ = 0;
  int bo_before_ = 0;            // others in the main room when they opened
  int last_others_ = 0;
  Decision final_;
};

// A reason the launcher accepts: [a-z0-9_]{1,40}.
bool valid_reason(const std::string& r);

}  // namespace athena::bot
