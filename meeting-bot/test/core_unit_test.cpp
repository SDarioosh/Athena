// Host tests of the core's parts: chunker, encoders, chat log, helpers. No SDK, no
// network. Fictional data only.
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <string>
#include <vector>

#include "chat.h"
#include "chunker.h"
#include "encoder.h"
#include "util.h"

#ifdef HAVE_OPUSFILE
#include <opusfile.h>
#endif

using namespace athena;

static int g_failures = 0;
#define CHECK(cond)                                                   \
  do {                                                                \
    if (!(cond)) {                                                    \
      std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                   \
    }                                                                 \
  } while (0)
#define CHECK_EQ(a, b)                                                       \
  do {                                                                       \
    auto va = (a);                                                           \
    auto vb = (b);                                                           \
    if (!(va == vb)) {                                                       \
      std::fprintf(stderr, "%s:%d: CHECK_EQ failed: %s != %s\n", __FILE__, __LINE__, #a, #b); \
      ++g_failures;                                                          \
    }                                                                        \
  } while (0)

namespace {

constexpr int kRate = 32000;          // what Zoom delivers
constexpr int kBlock = kRate / 100;   // 10 ms blocks, like the SDK
constexpr int64_t kT0 = 1760367600000LL;  // 2025-10-13T15:00:00Z, fictional meeting

// Counts samples instead of encoding, so chunk lengths are exact.
class CountingEncoder : public ChunkEncoder {
 public:
  bool write(const int16_t*, size_t n) override {
    samples_ += n;
    return true;
  }
  std::string finish() override { return std::to_string(samples_); }

 private:
  size_t samples_ = 0;
};

struct Harness {
  std::vector<FinishedChunk> out;
  TrackChunker chunker{30, [](int) { return std::make_unique<CountingEncoder>(); },
                       [this](FinishedChunk&& c) { out.push_back(std::move(c)); }};
  // `ms` of audio for one track in 10 ms blocks starting at `start`.
  void feed(const std::string& id, const std::string& name, int64_t start, int64_t ms,
            int rate = kRate) {
    std::vector<int16_t> block(static_cast<size_t>(rate / 100), 100);
    for (int64_t t = 0; t < ms; t += 10)
      chunker.add(id, name, block.data(), block.size(), rate, start + t);
  }
};

void test_util() {
  CHECK_EQ(util::json_str("a\"b\\c\nd\x01"), std::string("\"a\\\"b\\\\c\\nd\\u0001\""));
  CHECK_EQ(util::json_str("caf\xC3\xA9"), std::string("\"caf\xC3\xA9\""));
  CHECK_EQ(util::json_str("bad\xFFx"), std::string("\"bad\xEF\xBF\xBDx\""));
  CHECK_EQ(util::json_str("\xC3"), std::string("\"\xEF\xBF\xBD\""));  // cut sequence
  CHECK_EQ(util::utf8_truncate("\xC3\xA9\xC3\xA9\xC3\xA9", 2), std::string("\xC3\xA9\xC3\xA9"));
  CHECK_EQ(util::clean_line("  a\n\tb   c \r"), std::string("a b c"));
  CHECK_EQ(util::iso_utc_ms(kT0 + 21250), std::string("2025-10-13T15:00:21.250Z"));
  CHECK_EQ(util::epoch_seconds(kT0 + 5), std::string("1760367600.005"));
  CHECK(util::valid_track_id("16782336"));
  CHECK(!util::valid_track_id("a b"));
  CHECK(!util::valid_track_id(""));
  CHECK_EQ(util::sanitize_track_id("a b/c"), std::string("a_b_c"));
  CHECK_EQ(util::sanitize_track_id(""), std::string("unknown"));
  CHECK_EQ(util::sanitize_track_id(std::string(80, '1')).size(), size_t(64));
  CHECK(!util::plausible_epoch_ms(0));
  CHECK(util::plausible_epoch_ms(kT0));
  CHECK_EQ(util::url_escape("ab-1_2.c~/ x"), std::string("ab-1_2.c~%2F%20x"));
}

void test_chunk_boundaries_and_sequences() {
  Harness h;
  h.feed("100", "Dana Example", kT0, 70000);  // 70 s: 30 + 30 + (10 open)
  CHECK_EQ(h.out.size(), size_t(2));
  CHECK_EQ(h.chunker.open_chunks(), size_t(1));
  h.chunker.flush();  // the partial last chunk
  CHECK_EQ(h.out.size(), size_t(3));
  for (int i = 0; i < 3; ++i) {
    CHECK_EQ(h.out[i].track, std::string("100"));
    CHECK_EQ(h.out[i].track_name, std::string("Dana Example"));
    CHECK_EQ(h.out[i].sequence, i);
    CHECK_EQ(h.out[i].start_ms, kT0 + 30000LL * i);
    CHECK_EQ(h.out[i].sample_rate, kRate);
  }
  CHECK_EQ(h.out[0].samples, size_t(30 * kRate));
  CHECK_EQ(h.out[2].samples, size_t(10 * kRate));
  CHECK_EQ(h.out[2].data, std::to_string(10 * kRate));
  CHECK_EQ(h.chunker.open_chunks(), size_t(0));
}

void test_block_straddles_boundary() {
  Harness h;
  // 29.995 s, then one 10 ms block: 5 ms close the chunk, 5 ms start the next one.
  std::vector<int16_t> pcm(static_cast<size_t>(kRate * 29995 / 1000), 1);
  h.chunker.add("7", "Lee Example", pcm.data(), pcm.size(), kRate, kT0);
  std::vector<int16_t> block(kBlock, 1);
  h.chunker.add("7", "Lee Example", block.data(), block.size(), kRate, kT0 + 29995);
  CHECK_EQ(h.out.size(), size_t(1));
  CHECK_EQ(h.out[0].samples, size_t(30 * kRate));
  h.chunker.flush();
  CHECK_EQ(h.out.size(), size_t(2));
  CHECK_EQ(h.out[1].start_ms, kT0 + 30000);
  CHECK_EQ(h.out[1].samples, size_t(kBlock / 2));
  CHECK_EQ(h.out[1].sequence, 1);
}

void test_tracks_interleaved_keep_own_sequences() {
  Harness h;
  std::vector<int16_t> block(kBlock, 3);
  for (int64_t t = 0; t < 31000; t += 10) {
    h.chunker.add("1", "Dana Example", block.data(), block.size(), kRate, kT0 + t);
    if (t >= 5000)
      h.chunker.add("2", "Lee Example", block.data(), block.size(), kRate, kT0 + t);
  }
  h.chunker.flush();
  // track 1: 0..30 s, 30..31 s; track 2: 5..31 s
  CHECK_EQ(h.out.size(), size_t(3));
  int seq1 = 0, seq2 = 0;
  for (const auto& c : h.out) {
    if (c.track == "1") CHECK_EQ(c.sequence, seq1++);
    if (c.track == "2") {
      CHECK_EQ(c.sequence, seq2++);
      CHECK_EQ(c.start_ms, kT0 + 5000);
      CHECK_EQ(c.samples, size_t(26 * kRate));
    }
  }
  CHECK_EQ(seq1, 2);
  CHECK_EQ(seq2, 1);
}

void test_gap_starts_new_chunk() {
  Harness h;
  h.feed("5", "Dana Example", kT0, 4000);
  h.feed("5", "Dana Example", kT0 + 4000 + kGapToleranceMs, 1000);  // within tolerance
  CHECK_EQ(h.out.size(), size_t(0));
  h.feed("5", "Dana Example", kT0 + 20000, 2000);  // a real gap: not padded
  CHECK_EQ(h.out.size(), size_t(1));
  CHECK_EQ(h.out[0].samples, size_t(5 * kRate));
  CHECK_EQ(h.out[0].start_ms, kT0);
  h.chunker.flush();
  CHECK_EQ(h.out.size(), size_t(2));
  CHECK_EQ(h.out[1].start_ms, kT0 + 20000);
  CHECK_EQ(h.out[1].samples, size_t(2 * kRate));
  CHECK_EQ(h.out[1].sequence, 1);
}

void test_rejoin_keeps_track() {
  Harness h;
  h.feed("42", "Dana Example", kT0, 3000);
  h.chunker.close_track("42");  // left
  CHECK_EQ(h.out.size(), size_t(1));
  h.feed("42", "Dana E.", kT0 + 60000, 2000);  // back, renamed: same track
  h.chunker.flush();
  CHECK_EQ(h.out.size(), size_t(2));
  CHECK_EQ(h.out[1].track, std::string("42"));
  CHECK_EQ(h.out[1].sequence, 1);
  CHECK_EQ(h.out[1].track_name, std::string("Dana E."));
  CHECK_EQ(h.chunker.next_sequence("42"), 2);
  CHECK_EQ(h.chunker.next_sequence("43"), 0);
}

void test_rate_change_and_clock_back() {
  Harness h;
  h.feed("9", "Lee Example", kT0, 1000, 32000);
  h.feed("9", "Lee Example", kT0 + 1000, 1000, 16000);  // new rate: new chunk
  CHECK_EQ(h.out.size(), size_t(1));
  CHECK_EQ(h.out[0].sample_rate, 32000);
  h.feed("9", "Lee Example", kT0 - 60000, 1000, 16000);  // clock stepped back a minute
  CHECK_EQ(h.out.size(), size_t(2));
  CHECK_EQ(h.out[1].sample_rate, 16000);
  CHECK_EQ(h.out[1].start_ms, kT0 + 1000);
  h.feed("9", "Lee Example", kT0 - 59000 - 200, 500, 16000);  // jitter: appended
  CHECK_EQ(h.out.size(), size_t(2));
}

void test_close_stale() {
  Harness h;
  h.feed("1", "Dana Example", kT0, 2000);
  h.feed("2", "Lee Example", kT0, 10000);
  h.chunker.close_stale(kT0 + 10000);  // track 1 went quiet 8 s ago
  CHECK_EQ(h.out.size(), size_t(1));
  CHECK_EQ(h.out[0].track, std::string("1"));
  CHECK_EQ(h.chunker.open_chunks(), size_t(1));
}

void test_sanitized_track_and_bad_input() {
  Harness h;
  std::vector<int16_t> block(kBlock, 1);
  h.chunker.add("x y", "N", block.data(), block.size(), kRate, kT0);
  h.chunker.add("x y", "N", nullptr, 10, kRate, kT0);
  h.chunker.add("x y", "N", block.data(), 0, kRate, kT0);
  h.chunker.add("x y", "N", block.data(), block.size(), 0, kT0);
  h.chunker.flush();
  CHECK_EQ(h.out.size(), size_t(1));
  CHECK_EQ(h.out[0].track, std::string("x_y"));
}

std::vector<int16_t> sine(int rate, double seconds, double hz) {
  std::vector<int16_t> v(static_cast<size_t>(rate * seconds));
  for (size_t i = 0; i < v.size(); ++i)
    v[i] = static_cast<int16_t>(8000 * std::sin(2 * M_PI * hz * double(i) / rate));
  return v;
}

uint32_t le32(const std::string& s, size_t at) {
  uint32_t v = 0;
  for (int i = 3; i >= 0; --i) v = (v << 8) | static_cast<unsigned char>(s[at + i]);
  return v;
}

void test_wav_encoder() {
  auto pcm = sine(kRate, 1.5, 440);
  auto enc = make_encoder("wav", kRate);
  CHECK(enc != nullptr);
  enc->write(pcm.data(), 1000);
  enc->write(pcm.data() + 1000, pcm.size() - 1000);
  const std::string wav = enc->finish();
  CHECK_EQ(wav.size(), 44 + pcm.size() * 2);
  CHECK(wav.compare(0, 4, "RIFF") == 0);
  CHECK(wav.compare(8, 8, "WAVEfmt ") == 0);
  CHECK_EQ(le32(wav, 24), uint32_t(kRate));
  CHECK_EQ(le32(wav, 40), uint32_t(pcm.size() * 2));
  CHECK(std::memcmp(wav.data() + 44, pcm.data(), pcm.size() * 2) == 0);
}

// Last Ogg page's granule position (48 kHz samples including pre-skip).
int64_t last_granule(const std::string& ogg) {
  int64_t g = -1;
  for (size_t at = ogg.find("OggS"); at != std::string::npos; at = ogg.find("OggS", at + 4)) {
    if (at + 14 > ogg.size()) break;
    int64_t v = 0;
    for (int i = 7; i >= 0; --i) v = (v << 8) | static_cast<unsigned char>(ogg[at + 6 + i]);
    if (v >= 0) g = v;
  }
  return g;
}

void test_opus_encoder() {
  const double seconds = 3.0;
  auto pcm = sine(kRate, seconds, 440);
  auto enc = make_encoder("opus", kRate);
  CHECK(enc != nullptr);
  for (size_t i = 0; i < pcm.size(); i += kBlock) enc->write(pcm.data() + i, kBlock);
  const std::string ogg = enc->finish();
  CHECK(ogg.compare(0, 4, "OggS") == 0);
  const size_t head = ogg.find("OpusHead");
  CHECK(head != std::string::npos);
  CHECK_EQ(static_cast<int>(static_cast<unsigned char>(ogg[head + 9])), 1);  // mono
  const uint16_t preskip = static_cast<unsigned char>(ogg[head + 10]) |
                           static_cast<unsigned char>(ogg[head + 11]) << 8;
  CHECK_EQ(le32(ogg, head + 12), uint32_t(kRate));  // input rate recorded
  const int64_t g = last_granule(ogg);
  CHECK(std::llabs(g - preskip - int64_t(seconds * 48000)) < 48);  // within 1 ms
  // 32 kbps: about 4 KB per second, plus headers
  CHECK(ogg.size() > 6000 && ogg.size() < 20000);
  CHECK(make_encoder("mp3", kRate) == nullptr);
  CHECK(make_encoder("opus", 0) == nullptr);
#ifdef HAVE_OPUSFILE
  int err = 0;
  OggOpusFile* of = op_open_memory(reinterpret_cast<const unsigned char*>(ogg.data()),
                                   ogg.size(), &err);
  CHECK(of != nullptr);
  if (of) {
    std::vector<opus_int16> out(48000 * 4);
    size_t total = 0;
    double energy = 0;
    for (;;) {
      int n = op_read(of, out.data(), static_cast<int>(out.size()), nullptr);
      if (n <= 0) break;
      for (int i = 0; i < n; ++i) energy += double(out[i]) * out[i];
      total += static_cast<size_t>(n);
    }
    op_free(of);
    CHECK(std::llabs(int64_t(total) - int64_t(seconds * 48000)) < 48);
    const double rms = std::sqrt(energy / double(total ? total : 1));
    CHECK(rms > 3000 && rms < 8000);  // the sine came through (8000 peak: rms ~5657)
  }
#else
  std::fprintf(stderr, "note: libopusfile not found, Opus decode check skipped\n");
#endif
}

std::string read_file(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  return std::string(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
}

size_t count_lines(const std::string& s) {
  size_t n = 0;
  for (char c : s) n += c == '\n';
  return n;
}

void test_chat_log() {
  const auto dir = std::filesystem::temp_directory_path() / "athena-core-unit-chat";
  std::filesystem::remove_all(dir);
  std::filesystem::create_directories(dir);
  {
    ChatLog chat(dir.string());
    chat.on_chat(kT0 + 21000, "Dana Example", "16782336", "Here's the sheet: budget-v3.xlsx",
                 false);
    chat.on_chat(kT0 + 22000, "Lee Example", "16782337", "a secret for Athena only", true);
    chat.on_chat(kT0 + 23000, "Lee Example", "16782337", "  \n ", false);  // empty: skipped
    chat.on_chat(0, "Lee\nExample", "bad id!", "multi\nline", false);      // fixed up
    const std::string file = read_file((dir / "chat.jsonl").string());
    CHECK_EQ(count_lines(file), size_t(2));
    CHECK(file.find("secret") == std::string::npos);
    CHECK(file.find("\"zoom_user_id\": \"16782336\"") != std::string::npos);
    CHECK(file.find("bad id") == std::string::npos);
    CHECK(file.find("\"text\": \"multi line\"") != std::string::npos);
    CHECK(file.find("\"from\": \"Lee Example\"") != std::string::npos);
    CHECK(file.find("\"at\": \"1970") == std::string::npos);
    CHECK_EQ(chat.private_dropped(), size_t(1));

    chat.on_participant("16782336", "Dana Example", true, kT0 + 2000);
    chat.on_participant("16782336", "Dana E.", true, kT0 + 9000);  // rename
    chat.on_participant("16782337", "Lee Example", true, kT0 + 3000);
    chat.on_participant("16782337", "Lee Example", false, kT0 + 50000);
    chat.set_topic("Q4 budget\nreview");
    const std::string body = chat.body();
    CHECK(body.rfind("{\"lines\": [{\"at\": \"2025-10-13T15:00:21.000Z\"", 0) == 0);
    CHECK(body.find("\"topic\": \"Q4 budget review\"") != std::string::npos);
    CHECK(body.find("{\"zoom_user_id\": \"16782336\", \"name\": \"Dana E.\", \"joined_at\": "
                    "\"2025-10-13T15:00:02.000Z\", \"left_at\": null}") != std::string::npos);
    CHECK(body.find("\"left_at\": \"2025-10-13T15:00:50.000Z\"") != std::string::npos);
    const std::string side = read_file((dir / "participants.json").string());
    CHECK(side.find("Dana E.") != std::string::npos);
    chat.remove_files();
    CHECK(!std::filesystem::exists(dir / "chat.jsonl"));
    CHECK(!std::filesystem::exists(dir / "participants.json"));
  }
  {  // caps: 2,000 messages
    ChatLog chat(dir.string());
    for (int i = 0; i < 2100; ++i)
      chat.on_chat(kT0 + i * 1000, "Dana Example", "1", "message " + std::to_string(i), false);
    CHECK_EQ(chat.lines(), size_t(kMaxChatLines));
    CHECK_EQ(chat.dropped(), size_t(100));
    CHECK_EQ(count_lines(read_file((dir / "chat.jsonl").string())), size_t(kMaxChatLines));
    chat.remove_files();
  }
  {  // caps: 1 MB
    ChatLog chat(dir.string());
    const std::string big(5000, 'x');  // cut at 4,000 characters
    for (int i = 0; i < 400; ++i) chat.on_chat(kT0 + i, "Dana Example", "1", big, false);
    const std::string file = read_file((dir / "chat.jsonl").string());
    CHECK(file.size() <= kMaxChatFileBytes);
    CHECK(chat.lines() < 400 && chat.lines() > 200);
    CHECK(file.find(std::string(4001, 'x')) == std::string::npos);
    CHECK(chat.body().size() <= kMaxChatBodyBytes);
    chat.remove_files();
  }
  std::filesystem::remove_all(dir);
}

}  // namespace

int main() {
  test_util();
  test_chunk_boundaries_and_sequences();
  test_block_straddles_boundary();
  test_tracks_interleaved_keep_own_sequences();
  test_gap_starts_new_chunk();
  test_rejoin_keeps_track();
  test_rate_change_and_clock_back();
  test_close_stale();
  test_sanitized_track_and_bad_input();
  test_wav_encoder();
  test_opus_encoder();
  test_chat_log();
  if (g_failures) {
    std::fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
  }
  std::puts("core unit tests: ok");
  return 0;
}
