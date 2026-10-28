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

void Rules::left_meeting() {
  recording_ = false;
  no_audio_ = false;
  alone_since_ = 0;
  others_known_ = false;
}

void Rules::start_drop(int64_t now, bool by_sdk) {
  if (phase_ != Phase::Reconnecting) {  // a new drop: a new budget of tries and time
    drop_since_ = now;
    tries_ = 0;
  }
  left_meeting();
  phase_ = Phase::Reconnecting;
  sdk_reconnecting_ = by_sdk;
  attempt_ = false;
}

void Rules::waiting_room(int64_t now) {
  if (done() || phase_ == Phase::WaitingRoom) return;
  left_meeting();  // after joining: the host moved Athena back to the waiting room
  attempt_ = sdk_reconnecting_ = false;  // a rejoin try got this far: connected again
  phase_ = Phase::WaitingRoom;
  phase_since_ = now;
}

void Rules::waiting_for_host(int64_t now) {
  if (done() || phase_ == Phase::WaitingForHost) return;
  left_meeting();
  attempt_ = sdk_reconnecting_ = false;
  phase_ = Phase::WaitingForHost;
  phase_since_ = now;
}

void Rules::joined(int64_t now) {
  if (done()) return;
  if (phase_ != Phase::InMeeting) left_meeting();  // back in: recording starts again
  phase_ = Phase::InMeeting;
  attempt_ = sdk_reconnecting_ = false;
  tries_ = 0;
  drop_since_ = 0;
  if (!joined_ms_) joined_ms_ = now;
}

void Rules::reconnecting(int64_t now) {
  if (done()) return;
  if (phase_ == Phase::Reconnecting) {
    if (!attempt_) sdk_reconnecting_ = true;
    return;
  }
  // Before the first join the SDK's reconnect is part of joining; nothing to show.
  if (ever_joined()) start_drop(now, true);
}

Breakouts Rules::others(int count, int64_t now, bool breakouts_open) {
  if (done() || phase_ != Phase::InMeeting) return Breakouts::None;
  Breakouts ev = Breakouts::None;
  if (breakouts_open && !bo_open_) {
    bo_open_ = true;
    bo_noted_ = false;
    bo_since_ = now;
    // the main room just before they opened (this poll may already miss some)
    bo_before_ = others_known_ ? std::max(last_others_, count) : count;
  }
  if (bo_open_ && breakouts_open && !bo_noted_ && count * 2 < bo_before_) {
    bo_noted_ = true;  // "most participants": fewer than half are left in the main room
    ev = Breakouts::MostLeft;
  }
  if (!breakouts_open && bo_open_) {
    bo_open_ = false;
    if (bo_noted_) ev = Breakouts::Over;
    bo_noted_ = false;
  }
  last_others_ = count;
  others_known_ = true;
  if (count > 0 || bo_open_) {
    alone_since_ = 0;  // nobody is alone while breakout rooms are open
  } else if (!alone_since_) {
    alone_since_ = now;
  }
  return ev;
}

void Rules::recording(int64_t now) {
  if (done() || recording_) return;
  recording_ = true;
  recording_ms_ = now;
  no_audio_ = false;
}

void Rules::recording_stopped() {
  if (done()) return;
  recording_ = false;
  no_audio_ = false;
}

void Rules::permission_denied() {
  if (!recording_) denied_ = true;
}

Decision Rules::conclude(Decision d) {
  if (done()) return {};
  phase_ = Phase::Done;
  final_ = d;
  return d;
}

bool Rules::rejoin_due(int64_t now) const {
  return phase_ == Phase::Reconnecting && !sdk_reconnecting_ && !attempt_ &&
         tries_ < kRejoinTries && now >= drop_since_ + cfg_.rejoin_at_ms[tries_];
}

void Rules::rejoin_started(int64_t) {
  if (phase_ != Phase::Reconnecting) return;
  ++tries_;
  attempt_ = true;
}

Decision Rules::join_failed(JoinFail why, int64_t now) {
  if (done()) return {};
  if (phase_ == Phase::Reconnecting) {
    if (!attempt_) {  // the SDK gave up its own reconnect: our rejoin tries follow
      sdk_reconnecting_ = false;
      if (cfg_.obf && why == JoinFail::OwnerNotIn) return conclude(Decision::ended("owner_left"));
      return {};
    }
    attempt_ = false;  // our rejoin try failed
    switch (why) {
      case JoinFail::OwnerNotIn:  // obf: the owner is gone, so Zoom won't let us back
        return conclude(Decision::ended(cfg_.obf ? "owner_left" : "connection_lost"));
      case JoinFail::NotFound:
      case JoinFail::NotStarted:  // the meeting is over
        return conclude(Decision::ended("meeting_ended"));
      case JoinFail::WrongPasscode:
      case JoinFail::TokenExpired:  // will not change on another try
      case JoinFail::ExternalNotAllowed:
      case JoinFail::OutsideUsersBlocked:
      case JoinFail::BlockedByAdmin:
      case JoinFail::SignInRequired:
        return conclude(Decision::ended("connection_lost"));
      case JoinFail::Other:
        break;
    }
    if (tries_ >= kRejoinTries) return conclude(Decision::ended("connection_lost"));
    return {};
  }
  if (ever_joined()) {
    // Dropped after joining without the SDK reconnecting first. With an OBF token that
    // is Zoom ending the bot's session as the owner leaves (spike finding): owner_left,
    // no rejoin. A network drop shows MEETING_STATUS_RECONNECTING first (handled above).
    if (cfg_.obf) return conclude(Decision::ended("owner_left"));
    start_drop(now, false);
    return {};
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
    case JoinFail::ExternalNotAllowed:
      return conclude(Decision::failed("external_meeting_not_allowed"));
    case JoinFail::OutsideUsersBlocked: return conclude(Decision::failed("outside_users_blocked"));
    case JoinFail::BlockedByAdmin: return conclude(Decision::failed("blocked_by_account_admin"));
    case JoinFail::SignInRequired: return conclude(Decision::failed("zoom_signin_required"));
    case JoinFail::Other: break;
  }
  return conclude(Decision::failed("join_failed"));
}

Decision Rules::meeting_ended(EndKind why) {
  switch (why) {
    case EndKind::Removed: return conclude(Decision::ended("removed"));
    case EndKind::ByHost: return conclude(Decision::ended("meeting_ended"));
    case EndKind::HostNeverCame:
      return conclude(ever_joined() ? Decision::ended("meeting_ended")
                                    : Decision::failed("meeting_not_started"));
    case EndKind::Other: break;
  }
  if (phase_ == Phase::Reconnecting) {
    // Zoom closed the session while it reconnected or a rejoin try ran: still a drop.
    if (attempt_) {
      attempt_ = false;
      if (tries_ >= kRejoinTries) return conclude(Decision::ended("connection_lost"));
    }
    sdk_reconnecting_ = false;
    return {};
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
      if (now - phase_since_ >= cfg_.wait_ms)  // after joining: what was recorded stands
        return conclude(ever_joined() ? Decision::ended("waiting_room_timeout")
                                      : Decision::failed("waiting_room_timeout"));
      break;
    case Phase::WaitingForHost:
      if (now - phase_since_ >= cfg_.wait_ms)
        return conclude(ever_joined() ? Decision::ended("meeting_ended")
                                      : Decision::failed("meeting_not_started"));
      break;
    case Phase::WaitingForOwner:
      if (owner_wait_since_ && now - owner_wait_since_ >= cfg_.wait_ms)
        return conclude(Decision::failed("owner_not_joined"));
      break;
    case Phase::Reconnecting:
      if (now - drop_since_ >= cfg_.rejoin_window_ms)
        return conclude(Decision::ended("connection_lost"));
      break;
    case Phase::InMeeting:
      if (others_known_ && alone_since_ && now - alone_since_ >= cfg_.alone_ms)
        return conclude(Decision::ended("alone"));
      if (recording_) {
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
    case Phase::Reconnecting: return {"starting", "reconnecting"};
    case Phase::InMeeting:
      if (recording_) return {"recording", no_audio_ ? "no_audio" : ""};
      return {"waiting_permission", denied_ ? "denied" : ""};
    case Phase::Done:
      return {final_.outcome == Outcome::Failed ? "failed" : "ended", final_.reason};
  }
  return {};
}

}  // namespace athena::bot
