// Athena meeting bot core: chunker -> on-disk queue -> one uploader thread; chat log;
// finish(). See athena/core.h for the contract.
#include "athena/core.h"

#include <algorithm>
#include <atomic>
#include <cstdarg>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <mutex>
#include <thread>

#include "chat.h"
#include "chunker.h"
#include "encoder.h"
#include "http.h"
#include "util.h"

namespace athena {

namespace {

using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;

constexpr auto kBackoffFirst = std::chrono::seconds(1);
constexpr auto kBackoffMax = std::chrono::seconds(30);
constexpr long kUploadTimeoutMs = 120000;
constexpr long kStaleCheckEveryMs = 1000;
constexpr auto kEndCallsReserve = std::chrono::milliseconds(1000);

__attribute__((format(printf, 1, 2))) void logf(const char* fmt, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  std::fprintf(stderr, "athena-core: %s\n", buf);
}

CoreConfig normalized(CoreConfig c) {
  if (c.work_dir.empty()) c.work_dir = ".";
  if (c.max_session_seconds <= 0) c.max_session_seconds = 14400;
  if (c.chunk_seconds <= 0) c.chunk_seconds = 30;
  if (c.codec != "opus" && c.codec != "wav") {
    logf("unknown codec %s, using opus", c.codec.c_str());
    c.codec = "opus";
  }
  return c;
}

long ms_until(Clock::time_point t) {
  const auto d = std::chrono::duration_cast<std::chrono::milliseconds>(t - Clock::now());
  return std::max<long>(1, static_cast<long>(d.count()));
}

struct QueuedChunk {
  std::string path, track, track_name;
  int sequence = 0;
  int64_t start_ms = 0;
  double seconds = 0;
  bool in_flight = false;
};

}  // namespace

struct Core::Impl {
  explicit Impl(CoreConfig c)
      : cfg(normalized(std::move(c))),
        queue_dir(cfg.work_dir + "/queue"),
        chat(cfg.work_dir),
        chunker(
            cfg.chunk_seconds,
            [codec = cfg.codec](int rate) { return make_encoder(codec, rate); },
            [this](FinishedChunk&& ch) { enqueue(std::move(ch)); }) {
    std::error_code ec;
    fs::create_directories(queue_dir, ec);
    if (ec) logf("could not create %s: %s", queue_dir.c_str(), ec.message().c_str());
  }

  // ---- queue (chunker thread side) ---------------------------------------------------
  void enqueue(FinishedChunk&& c) {
    QueuedChunk q;
    q.track = c.track;
    q.track_name = util::utf8_truncate(util::clean_line(c.track_name), kMaxNameChars);
    q.sequence = c.sequence;
    q.start_ms = c.start_ms;
    q.seconds = c.seconds();
    q.path = queue_dir + "/" + c.track + "-" + std::to_string(c.sequence) + "." +
             codec_extension(cfg.codec);
    const std::string part = q.path + ".part";
    {
      std::ofstream f(part, std::ios::binary | std::ios::trunc);
      f.write(c.data.data(), static_cast<std::streamsize>(c.data.size()));
      f.close();
      if (!f || std::rename(part.c_str(), q.path.c_str()) != 0) {
        std::remove(part.c_str());
        logf("could not write the queue file %s, chunk dropped", q.path.c_str());
        std::lock_guard<std::mutex> lock(q_mu);
        ++dropped;
        return;
      }
    }
    std::lock_guard<std::mutex> lock(q_mu);
    queue.push_back(std::move(q));
    queued_seconds += queue.back().seconds;
    ++chunks_per_track[queue.back().track];
    // Cap on unsent audio: drop the oldest chunk not being uploaded right now.
    while (queued_seconds > cfg.max_session_seconds && queue.size() > 1) {
      auto victim = std::find_if(queue.begin(), std::prev(queue.end()),
                                 [](const QueuedChunk& x) { return !x.in_flight; });
      if (victim == std::prev(queue.end())) break;
      logf("queue over the cap of %d s of unsent audio, dropped %s", cfg.max_session_seconds,
           victim->path.c_str());
      drop_locked(victim);
    }
    q_cv.notify_all();
  }

  void drop_locked(std::deque<QueuedChunk>::iterator it) {
    std::remove(it->path.c_str());
    queued_seconds -= it->seconds;
    ++dropped;
    queue.erase(it);
  }

  // ---- uploader thread ---------------------------------------------------------------
  bool cut_passed() const {
    const int64_t c = cutoff_ns.load();
    return c != 0 && Clock::now().time_since_epoch().count() >= c;
  }
  bool must_stop() const { return stop.load() || cut_passed(); }

  void upload_loop() {
    auto backoff = std::chrono::duration_cast<Clock::duration>(kBackoffFirst);
    Clock::time_point next_try{};
    std::unique_lock<std::mutex> lk(q_mu);
    for (;;) {
      while (!must_stop() && (queue.empty() || Clock::now() < next_try)) {
        Clock::time_point until = queue.empty() ? Clock::time_point::max() : next_try;
        if (const int64_t c = cutoff_ns.load())
          until = std::min(until, Clock::time_point(Clock::duration(c)));
        if (until == Clock::time_point::max())
          q_cv.wait(lk);
        else
          q_cv.wait_until(lk, until);
      }
      if (must_stop()) break;
      queue.front().in_flight = true;
      const QueuedChunk item = queue.front();
      lk.unlock();

      std::string data;
      {
        std::ifstream f(item.path, std::ios::binary);
        data.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
      }
      HttpResponse r;
      bool unreadable = data.empty();
      if (!unreadable) {
        std::vector<std::pair<std::string, std::string>> fields = {
            {"device", cfg.device},
            {"session_id", cfg.session_id},
            {"sequence", std::to_string(item.sequence)},
            {"timestamp", util::epoch_seconds(item.start_ms)},
            {"codec", cfg.codec},
            {"track", item.track},
        };
        if (!item.track_name.empty()) fields.emplace_back("track_name", item.track_name);
        Http::FilePart file{"audio", fs::path(item.path).filename().string(),
                            codec_mime(cfg.codec), std::move(data)};
        r = http.post_multipart(join_url(cfg.upload_url, "/v1/ingest/chunk"),
                                cfg.upload_token, fields, file, kUploadTimeoutMs,
                                [this] { return must_stop(); });
      }

      lk.lock();
      queue.front().in_flight = false;  // still the front: nothing else removes in-flight
      if (unreadable) {
        logf("could not read the queue file %s, chunk dropped", item.path.c_str());
        drop_locked(queue.begin());
      } else if (r.status == 200 && r.json_true("ok")) {
        std::remove(item.path.c_str());
        queued_seconds -= item.seconds;
        queue.pop_front();
        ++uploaded;
        backoff = std::chrono::duration_cast<Clock::duration>(kBackoffFirst);
        next_try = {};
        if (r.json_true("stop") && !stop_requested.exchange(true))
          logf("the server asked to stop (%s)", r.body.substr(0, 200).c_str());
      } else if (r.status == 401 || r.status == 403) {
        rejected.store(true);
        logf("upload token rejected (HTTP %ld), uploads stop", r.status);
        break;
      } else if (r.aborted) {
        break;
      } else if (r.status == 0 || r.status == 429 || r.status >= 500 || r.status == 200) {
        logf("upload of %s failed (%s), retrying in %lld s", item.path.c_str(),
             r.status ? ("HTTP " + std::to_string(r.status)).c_str() : r.error.c_str(),
             static_cast<long long>(
                 std::chrono::duration_cast<std::chrono::seconds>(backoff).count()));
        next_try = Clock::now() + backoff;
        backoff = std::min<Clock::duration>(backoff * 2, kBackoffMax);
      } else {
        logf("server refused %s, dropped: HTTP %ld %s", item.path.c_str(), r.status,
             r.body.substr(0, 200).c_str());
        drop_locked(queue.begin());
      }
      q_cv.notify_all();
    }
    uploader_done = true;
    q_cv.notify_all();
  }

  CoreConfig cfg;
  std::string queue_dir;
  ChatLog chat;

  std::mutex chunk_mu;  // before q_mu when both are held
  TrackChunker chunker;
  int64_t last_stale_check_ms = 0;
  bool closed = false;  // finish() started: audio is no longer taken

  std::mutex q_mu;
  std::condition_variable q_cv;
  std::deque<QueuedChunk> queue;
  double queued_seconds = 0;
  size_t uploaded = 0, dropped = 0;
  std::map<std::string, int> chunks_per_track;
  bool uploader_done = false;

  std::atomic<bool> stop{false};
  std::atomic<int64_t> cutoff_ns{0};  // steady clock; 0 = none
  std::atomic<bool> stop_requested{false};
  std::atomic<bool> rejected{false};

  std::mutex finish_mu;
  bool finished = false;
  bool finish_result = false;

  Http http;  // the uploader's until it is joined, then finish()'s
  std::thread uploader;
};

Core::Core(CoreConfig cfg) : impl_(new Impl(std::move(cfg))) {
  impl_->uploader = std::thread([this] { impl_->upload_loop(); });
}

Core::~Core() {
  if (impl_->uploader.joinable()) {
    impl_->stop.store(true);
    {
      std::lock_guard<std::mutex> lock(impl_->q_mu);
      impl_->q_cv.notify_all();
    }
    impl_->uploader.join();
  }
}

void Core::on_audio(const std::string& track_id, const std::string& track_name,
                    const int16_t* pcm, size_t samples, int sample_rate, int64_t now_ms) {
  Impl& d = *impl_;
  std::lock_guard<std::mutex> lock(d.chunk_mu);
  if (d.closed) return;
  d.chunker.add(track_id, track_name, pcm, samples, sample_rate, now_ms);
  if (now_ms - d.last_stale_check_ms >= kStaleCheckEveryMs) {
    d.last_stale_check_ms = now_ms;
    d.chunker.close_stale(now_ms);
  }
}

void Core::on_participant(const std::string& track_id, const std::string& name, bool joined,
                          int64_t at_ms) {
  impl_->chat.on_participant(track_id, name, joined, at_ms);
  if (!joined) {
    std::lock_guard<std::mutex> lock(impl_->chunk_mu);
    if (!impl_->closed) impl_->chunker.close_track(track_id);
  }
}

void Core::on_chat(int64_t at_ms, const std::string& from_name,
                   const std::string& from_track_id, const std::string& text,
                   bool private_to_bot) {
  impl_->chat.on_chat(at_ms, from_name, from_track_id, text, private_to_bot);
}

void Core::set_topic(const std::string& topic) { impl_->chat.set_topic(topic); }

bool Core::stop_requested() const { return impl_->stop_requested.load(); }
bool Core::upload_rejected() const { return impl_->rejected.load(); }

bool Core::finish(std::chrono::steady_clock::time_point deadline) {
  Impl& d = *impl_;
  std::lock_guard<std::mutex> once(d.finish_mu);
  if (d.finished) return d.finish_result;
  d.finished = true;

  {  // the last, partial chunks
    std::lock_guard<std::mutex> lock(d.chunk_mu);
    d.closed = true;
    d.chunker.flush();
  }

  // Chunks first, then the chat call, then /end; keep a little time for the two calls.
  const auto now = Clock::now();
  const auto left = deadline > now ? deadline - now : Clock::duration::zero();
  const auto reserve = std::min<Clock::duration>(kEndCallsReserve, left / 3);
  const auto chunk_cutoff = deadline - reserve;
  const auto chat_cutoff = deadline - reserve / 2;

  size_t unsent = 0, dropped = 0, uploaded = 0;
  {
    std::unique_lock<std::mutex> lk(d.q_mu);
    d.cutoff_ns.store(std::max<int64_t>(1, chunk_cutoff.time_since_epoch().count()));
    d.q_cv.notify_all();
    d.q_cv.wait_until(lk, chunk_cutoff,
                      [&] { return d.queue.empty() || d.uploader_done; });
    d.stop.store(true);
    d.q_cv.notify_all();
  }
  d.uploader.join();
  {
    std::lock_guard<std::mutex> lk(d.q_mu);
    unsent = d.queue.size();
    dropped = d.dropped;
    uploaded = d.uploaded;
    for (const auto& [track, n] : d.chunks_per_track)
      std::fprintf(stderr, "athena-core: track %s: %d chunks\n", track.c_str(), n);
  }
  std::fprintf(stderr, "athena-core: %zu chunks uploaded, %zu unsent, %zu dropped\n",
               uploaded, unsent, dropped);
  if (d.rejected.load()) {
    logf("upload token rejected: no chat or end call");
    return d.finish_result = false;
  }

  const std::string sid = util::url_escape(d.cfg.session_id);
  bool chat_ok = true;
  const std::string body = d.chat.body();
  if (body != "{\"lines\": []}") {
    const HttpResponse r = d.http.post_json(
        join_url(d.cfg.upload_url, "/v1/sessions/" + sid + "/chat"), d.cfg.upload_token,
        body, ms_until(chat_cutoff), [&] { return Clock::now() >= chat_cutoff; });
    chat_ok = r.status == 200;
    if (!chat_ok)
      logf("chat call failed: %s %s",
           r.status ? ("HTTP " + std::to_string(r.status)).c_str() : r.error.c_str(),
           r.body.substr(0, 200).c_str());
  }
  const HttpResponse r = d.http.post_json(
      join_url(d.cfg.upload_url, "/v1/sessions/" + sid + "/end"), d.cfg.upload_token, "",
      ms_until(deadline), [&] { return Clock::now() >= deadline; });
  // Nothing was ever uploaded: the server has no such session, nothing to end.
  const bool end_ok = r.status == 200 || (r.status == 404 && uploaded == 0);
  if (!end_ok)
    logf("end call failed: %s",
         r.status ? ("HTTP " + std::to_string(r.status)).c_str() : r.error.c_str());

  d.finish_result = unsent == 0 && dropped == 0 && chat_ok && end_ok;
  if (d.finish_result) {
    d.chat.remove_files();
    std::error_code ec;
    fs::remove(d.queue_dir, ec);  // empty by now
  }
  return d.finish_result;
}

}  // namespace athena
