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
};

// SDK-neutral join failure (main.cpp maps MeetingFailCode to it).
enum class JoinFail { WrongPasscode, NotStarted, NotFound, TokenExpired, OwnerNotIn, Other };
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

enum class Phase { Joining, WaitingForOwner, WaitingForHost, WaitingRoom, InMeeting, Done };

class Rules {
 public:
  Rules(RulesConfig cfg, int64_t start_ms);

  // Zoom put us in the waiting room / waiting for the host to start the meeting.
  void waiting_room(int64_t now);
  void waiting_for_host(int64_t now);
  // Fully in the meeting (again, after a reconnect: timers keep running).
  void joined(int64_t now);
  // Participants other than the bot, from the poll of the participant list.
  void others(int count, int64_t now);
  // Raw audio subscribed: recording.
  void recording(int64_t now);
  // The host declined the one recording request.
  void permission_denied();

  // A join attempt (or the session after joining) failed. None = retry the join (obf,
  // owner not in yet, within the wait): main.cpp tries again in 3 s.
  Decision join_failed(JoinFail why, int64_t now);
  // Zoom ended our meeting session (not because we left).
  Decision meeting_ended(EndKind why);
  // SIGTERM / SIGINT.
  Decision stop_signal();
  // Every second while the bot runs. last_audio_ms = the newest raw audio frame (0: none).
  Decision tick(int64_t now, int64_t last_audio_ms, bool server_stop, bool upload_rejected);

  // What the status line should say now.
  Status status() const;
  Phase phase() const { return phase_; }
  bool done() const { return phase_ == Phase::Done; }
  bool ever_joined() const { return joined_ms_ != 0; }
  bool is_recording() const { return recording_ms_ != 0; }

 private:
  Decision conclude(Decision d);

  RulesConfig cfg_;
  int64_t start_ms_;
  Phase phase_ = Phase::Joining;
  int64_t phase_since_ = 0;      // waiting room / host wait start
  int64_t owner_wait_since_ = 0; // first refusal because the owner wasn't in
  int64_t joined_ms_ = 0;
  int64_t alone_since_ = 0;      // 0 = someone else is here (or not known yet)
  bool others_known_ = false;
  int64_t recording_ms_ = 0;
  bool denied_ = false;
  bool no_audio_ = false;
  Decision final_;
};

// A reason the launcher accepts: [a-z0-9_]{1,40}.
bool valid_reason(const std::string& r);

}  // namespace athena::bot
