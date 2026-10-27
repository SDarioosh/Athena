#include "participants.h"

namespace athena::bot {

std::vector<ParticipantChange> ParticipantTracker::update(
    const std::map<unsigned int, std::string>& snapshot) {
  std::vector<ParticipantChange> changes;
  for (const auto& [id, name] : snapshot) {
    auto it = present_.find(id);
    if (it == present_.end() || it->second != name)
      changes.push_back({std::to_string(id), name, true});
    known_[id] = name;
  }
  for (const auto& [id, name] : present_)
    if (!snapshot.count(id)) changes.push_back({std::to_string(id), name, false});
  present_ = snapshot;
  return changes;
}

std::string ParticipantTracker::name_of(unsigned int user_id) const {
  auto it = known_.find(user_id);
  return it == known_.end() ? "" : it->second;
}

}  // namespace athena::bot
