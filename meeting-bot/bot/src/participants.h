// Turns successive snapshots of the meeting's participant list (polled once a second while
// in the meeting) into join / leave / rename events for Core::on_participant. No SDK here.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace athena::bot {

struct ParticipantChange {
  std::string track_id;  // Zoom user id, decimal
  std::string name;
  bool joined;           // true: joined or renamed (joined again with the new name)
};

class ParticipantTracker {
 public:
  // snapshot = everyone in the meeting except the bot itself (user id -> display name).
  std::vector<ParticipantChange> update(const std::map<unsigned int, std::string>& snapshot);
  size_t count() const { return present_.size(); }
  // The last known name of a user id ("" if never seen).
  std::string name_of(unsigned int user_id) const;

 private:
  std::map<unsigned int, std::string> present_;
  std::map<unsigned int, std::string> known_;  // also those who left
};

}  // namespace athena::bot
