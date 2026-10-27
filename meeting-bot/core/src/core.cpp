// Skeleton: compiles, does nothing yet. The real implementation follows.
#include "athena/core.h"

#include <atomic>

namespace athena {

struct Core::Impl {
  CoreConfig cfg;
};

Core::Core(CoreConfig cfg) : impl_(new Impl{std::move(cfg)}) {}
Core::~Core() = default;
void Core::on_audio(const std::string&, const std::string&, const int16_t*, size_t, int,
                    int64_t) {}
void Core::on_participant(const std::string&, const std::string&, bool, int64_t) {}
void Core::on_chat(int64_t, const std::string&, const std::string&, const std::string&,
                   bool) {}
void Core::set_topic(const std::string&) {}
bool Core::stop_requested() const { return false; }
bool Core::upload_rejected() const { return false; }
bool Core::finish(std::chrono::steady_clock::time_point) { return true; }

}  // namespace athena
