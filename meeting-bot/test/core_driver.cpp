// Drives athena::Core like the bot would, for test_core_upload.py: a fictional meeting
// (two participants, chat, topic), then finish(). Prints key=value results on stdout.
//
//   core_driver --url http://127.0.0.1:PORT --session S --token T --work DIR
//               [--codec opus|wav] [--audio-seconds 70] [--max-seconds 14400]
//               [--wait-ms 0] [--deadline-ms 4000] [--chat-flood]
//               [--pattern meeting|bursts|tail]
//   meeting: Dana talks throughout, Lee 5..15 s and 40..45 s (leaves in between)
//   bursts:  Dana alone, 1 s of speech every 10 s
//   tail:    Dana alone, 0..29.5 s, then silence (the uploader's tick closes the chunk)
//   chat:    no audio at all (no recording permission), one chat line
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include "athena/core.h"

namespace {

constexpr int kRate = 32000;
constexpr int kBlock = kRate / 100;
constexpr int64_t kT0 = 1760367600000LL;  // 2025-10-13T15:00:00Z, a fictional meeting
const char* kDana = "16782336";
const char* kLee = "16782337";

size_t count_files(const std::string& dir) {
  size_t n = 0;
  std::error_code ec;
  for (const auto& e : std::filesystem::directory_iterator(dir, ec))
    if (e.path().extension() != ".part") ++n;
  return n;
}

size_t count_lines(const std::string& path) {
  std::ifstream f(path);
  size_t n = 0;
  std::string line;
  while (std::getline(f, line)) ++n;
  return n;
}

}  // namespace

int main(int argc, char** argv) {
  athena::CoreConfig cfg;
  cfg.codec = "opus";
  int audio_seconds = 70, wait_ms = 0, deadline_ms = 4000;
  bool chat_flood = false;
  std::string pattern = "meeting";
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() -> std::string { return i + 1 < argc ? argv[++i] : ""; };
    if (a == "--url") cfg.upload_url = next();
    else if (a == "--session") cfg.session_id = next();
    else if (a == "--token") cfg.upload_token = next();
    else if (a == "--work") cfg.work_dir = next();
    else if (a == "--codec") cfg.codec = next();
    else if (a == "--audio-seconds") audio_seconds = std::atoi(next().c_str());
    else if (a == "--max-seconds") cfg.max_session_seconds = std::atoi(next().c_str());
    else if (a == "--wait-ms") wait_ms = std::atoi(next().c_str());
    else if (a == "--deadline-ms") deadline_ms = std::atoi(next().c_str());
    else if (a == "--chat-flood") chat_flood = true;
    else if (a == "--pattern") pattern = next();
    else {
      std::fprintf(stderr, "unknown argument %s\n", a.c_str());
      return 2;
    }
  }

  athena::Core core(cfg);
  core.on_participant(kDana, "Dana Example", true, kT0 + 2000);
  core.on_participant(kLee, "Lee Example", true, kT0 + 3000);
  core.set_topic("Q4 budget review");

  // Dana talks the whole time; Lee talks 5..15 s, leaves, comes back at 40 s for 5 s.
  std::vector<int16_t> dana(kBlock), lee(kBlock);
  for (int i = 0; i < kBlock; ++i) {
    dana[i] = static_cast<int16_t>(6000 * std::sin(2 * M_PI * 220 * i / kRate));
    lee[i] = static_cast<int16_t>(6000 * std::sin(2 * M_PI * 330 * i / kRate));
  }
  int64_t total_ms = pattern == "tail" ? 29500 : int64_t(audio_seconds) * 1000;
  if (pattern == "chat") {
    total_ms = 0;
    core.on_chat(kT0 + 21000, "Dana Example", kDana,
                 "Here's the sheet: https://example.invalid/budget-v3.xlsx", false);
  }
  const bool meeting = pattern == "meeting";
  for (int64_t t = 0; t < total_ms; t += 10) {
    const int64_t now = kT0 + t;
    if (pattern != "bursts" || t % 10000 < 1000)
      core.on_audio(kDana, t < 50000 ? "Dana Example" : "Dana E.", dana.data(), dana.size(),
                    kRate, now);
    if (!meeting) continue;
    if ((t >= 5000 && t < 15000) || (t >= 40000 && t < 45000))
      core.on_audio(kLee, "Lee Example", lee.data(), lee.size(), kRate, now);
    if (t == 15000) core.on_participant(kLee, "Lee Example", false, now);
    if (t == 40000) core.on_participant(kLee, "Lee Example", true, now);
    if (t == 50000) core.on_participant(kDana, "Dana E.", true, now);  // rename
    if (t == 21000)
      core.on_chat(now, "Dana Example", kDana, "Here's the sheet: https://example.invalid/budget-v3.xlsx",
                   false);
    if (t == 22000)
      core.on_chat(now, "Lee Example", kLee, "PRIVATE-NOTE-FOR-ATHENA", true);
    if (t == 23000) core.on_chat(now, "Lee Example", kLee, "Thanks,\nlooks good", false);
  }
  if (chat_flood) {
    for (int i = 0; i < 2100; ++i)
      core.on_chat(kT0 + 60000 + i, "Lee Example", kLee, "flood " + std::to_string(i), false);
    for (int i = 0; i < 5; ++i)
      core.on_chat(kT0 + 70000 + i, "Lee Example", kLee, "PRIVATE-NOTE-FOR-ATHENA", true);
  }
  std::printf("queue_files_before_finish=%zu\n", count_files(cfg.work_dir + "/queue"));
  std::printf("chat_lines_before_finish=%zu\n", count_lines(cfg.work_dir + "/chat.jsonl"));

  const auto wait_end = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait_ms);
  while (std::chrono::steady_clock::now() < wait_end && !core.stop_requested() &&
         !core.upload_rejected())
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  std::printf("stop=%d\nrejected=%d\n", core.stop_requested() ? 1 : 0,
              core.upload_rejected() ? 1 : 0);

  std::printf("finish_wall=%.3f\n",
              std::chrono::duration<double>(
                  std::chrono::system_clock::now().time_since_epoch()).count());
  const auto start = std::chrono::steady_clock::now();
  const bool ok = core.finish(start + std::chrono::milliseconds(deadline_ms));
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - start);
  std::printf("finish=%d\nelapsed_ms=%lld\n", ok ? 1 : 0,
              static_cast<long long>(elapsed.count()));
  std::fflush(stdout);
  return 0;
}
