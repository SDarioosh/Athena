#include "rules.h"

#include <algorithm>

namespace athena::bot {

bool valid_reason(const std::string& r) {
  if (r.empty() || r.size() > 40) return false;
  return std::all_of(r.begin(), r.end(), [](char c) {
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_';
  });
}

Rules::Rules(RulesConfig cfg, int64_t start_ms) : cfg_(cfg), start_ms_(start_ms) {}

void Rules::waiting_room(int64_t now) {
  if (done() || phase_ == Phase::WaitingRoom) return;
  phase_ = Phase::WaitingRoom;
  phase_since_ = now;
}

void Rules::waiting_for_host(int64_t now) {
  if (done() || phase_ == Phase::WaitingForHost) return;
  phase_ = Phase::WaitingForHost;
  phase_since_ = now;
}

void Rules::joined(int64_t now) {
  if (done()) return;
  phase_ = Phase::InMeeting;
  if (!joined_ms_) joined_ms_ = now;
}

void Rules::others(int count, int64_t now) {
  if (done() || phase_ != Phase::InMeeting) return;
  others_known_ = true;
  if (count > 0) {
    alone_since_ = 0;
  } else if (!alone_since_) {
    alone_since_ = now;
  }
}

void Rules::recording(int64_t now) {
  if (done() || recording_ms_) return;
  recording_ms_ = now;
}

void Rules::permission_denied() {
  if (!recording_ms_) denied_ = true;
}

Decision Rules::conclude(Decision d) {
  if (done()) return {};
  phase_ = Phase::Done;
  final_ = d;
  return d;
}

Decision Rules::join_failed(JoinFail why, int64_t now) {
  if (done()) return {};
  if (phase_ == Phase::InMeeting) {
    // Dropped after joining. With an OBF token Zoom ends the bot's session when the owner
    // leaves (spike finding); without one it is a lost connection Zoom gave up on.
    return conclude(Decision::ended(cfg_.obf ? "owner_left" : "connection_lost"));
  }
  switch (why) {
    case JoinFail::OwnerNotIn:
      if (!cfg_.obf) return conclude(Decision::failed("join_failed"));
      if (!owner_wait_since_) owner_wait_since_ = now;
      if (now - owner_wait_since_ >= cfg_.wait_ms)
        return conclude(Decision::failed("owner_not_joined"));
      phase_ = Phase::WaitingForOwner;
      return {};
    case JoinFail::WrongPasscode: return conclude(Decision::failed("wrong_passcode"));
    case JoinFail::NotStarted: return conclude(Decision::failed("meeting_not_started"));
    case JoinFail::NotFound: return conclude(Decision::failed("meeting_not_found"));
    case JoinFail::TokenExpired: return conclude(Decision::failed("join_token_expired"));
    case JoinFail::Other: break;
  }
  return conclude(Decision::failed("join_failed"));
}

Decision Rules::meeting_ended(EndKind why) {
  switch (why) {
    case EndKind::Removed: return conclude(Decision::ended("removed"));
    case EndKind::ByHost: return conclude(Decision::ended("meeting_ended"));
    case EndKind::HostNeverCame: return conclude(Decision::failed("meeting_not_started"));
    case EndKind::Other: break;
  }
  // Not ended by the host: with OBF that is Zoom dropping us as the owner left (spike).
  return conclude(Decision::ended(cfg_.obf && phase_ == Phase::InMeeting ? "owner_left"
                                                                         : "meeting_ended"));
}

Decision Rules::stop_signal() { return conclude(Decision::ended("stopped")); }

Decision Rules::tick(int64_t now, int64_t last_audio_ms, bool server_stop,
                     bool upload_rejected) {
  if (done()) return {};
  const bool over_max = now - start_ms_ >= cfg_.max_ms;
  // The upload token is gone: the launcher deleted the meeting (it also sends SIGTERM).
  if (upload_rejected) return conclude(Decision::ended("stopped"));
  // The server answered "stop": true (max duration or the session was ended).
  if (server_stop) return conclude(Decision::ended(over_max ? "max_duration" : "server_stop"));
  if (over_max) return conclude(Decision::ended("max_duration"));
  switch (phase_) {
    case Phase::WaitingRoom:
      if (now - phase_since_ >= cfg_.wait_ms)
        return conclude(Decision::failed("waiting_room_timeout"));
      break;
    case Phase::WaitingForHost:
      if (now - phase_since_ >= cfg_.wait_ms)
        return conclude(Decision::failed("meeting_not_started"));
      break;
    case Phase::WaitingForOwner:
      if (owner_wait_since_ && now - owner_wait_since_ >= cfg_.wait_ms)
        return conclude(Decision::failed("owner_not_joined"));
      break;
    case Phase::InMeeting:
      if (others_known_ && alone_since_ && now - alone_since_ >= cfg_.alone_ms)
        return conclude(Decision::ended("alone"));
      if (recording_ms_) {
        int64_t last = std::max(last_audio_ms, recording_ms_);
        no_audio_ = now - last >= cfg_.no_audio_ms;
      }
      break;
    case Phase::Joining:
    case Phase::Done:
      break;
  }
  return {};
}

Status Rules::status() const {
  switch (phase_) {
    case Phase::Joining: return {};
    case Phase::WaitingForOwner: return {"starting", "waiting_for_owner"};
    case Phase::WaitingForHost: return {"starting", "waiting_for_host"};
    case Phase::WaitingRoom: return {"waiting_room", ""};
    case Phase::InMeeting:
      if (recording_ms_) return {"recording", no_audio_ ? "no_audio" : ""};
      return {"waiting_permission", denied_ ? "denied" : ""};
    case Phase::Done:
      return {final_.outcome == Outcome::Failed ? "failed" : "ended", final_.reason};
  }
  return {};
}

}  // namespace athena::bot
