#include "http.h"

#include <curl/curl.h>

#include <algorithm>
#include <mutex>
#include <regex>

namespace athena {

namespace {

constexpr size_t kMaxResponseBytes = 64 * 1024;

size_t on_body(char* ptr, size_t size, size_t nmemb, void* user) {
  auto* out = static_cast<std::string*>(user);
  const size_t n = size * nmemb;
  if (out->size() < kMaxResponseBytes) out->append(ptr, std::min(n, kMaxResponseBytes - out->size()));
  return n;
}

void global_init() {
  static std::once_flag once;
  std::call_once(once, [] { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

void common_options(CURL* e, const std::string& url, long timeout_ms, std::string* body,
                    curl_slist* headers) {
  curl_easy_setopt(e, CURLOPT_URL, url.c_str());
  curl_easy_setopt(e, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(e, CURLOPT_PROXY, "");  // never through a proxy: straight to Athena
  curl_easy_setopt(e, CURLOPT_PROTOCOLS_STR, "http,https");
  curl_easy_setopt(e, CURLOPT_FOLLOWLOCATION, 0L);
  curl_easy_setopt(e, CURLOPT_CONNECTTIMEOUT_MS, std::min(timeout_ms, 10000L));
  curl_easy_setopt(e, CURLOPT_TIMEOUT_MS, std::max(timeout_ms, 1L));
  curl_easy_setopt(e, CURLOPT_HTTPHEADER, headers);
  curl_easy_setopt(e, CURLOPT_WRITEFUNCTION, on_body);
  curl_easy_setopt(e, CURLOPT_WRITEDATA, body);
  curl_easy_setopt(e, CURLOPT_USERAGENT, "athena-meeting-bot");
}

curl_slist* auth_headers(const std::string& token, bool json) {
  curl_slist* h = nullptr;
  h = curl_slist_append(h, ("Authorization: Bearer " + token).c_str());
  h = curl_slist_append(h, "Expect:");  // no 100-continue round trip
  if (json) h = curl_slist_append(h, "Content-Type: application/json");
  return h;
}

}  // namespace

bool HttpResponse::json_true(const char* key) const {
  const std::regex re(std::string("\"") + key + "\"\\s*:\\s*true\\b");
  return std::regex_search(body, re);
}

Http::Http() {
  global_init();
  multi_ = curl_multi_init();
}

Http::~Http() {
  if (multi_) curl_multi_cleanup(static_cast<CURLM*>(multi_));
}

HttpResponse Http::perform(void* easy, const Abort& abort) {
  HttpResponse r;
  auto* m = static_cast<CURLM*>(multi_);
  auto* e = static_cast<CURL*>(easy);
  if (!m) {
    r.error = "curl_multi_init failed";
    return r;
  }
  if (abort && abort()) {
    r.aborted = true;
    r.error = "aborted";
    return r;
  }
  curl_multi_add_handle(m, e);
  CURLcode result = CURLE_OK;
  bool done = false;
  for (;;) {
    int running = 0;
    if (curl_multi_perform(m, &running) != CURLM_OK) {
      r.error = "curl_multi_perform failed";
      break;
    }
    int left = 0;
    while (CURLMsg* msg = curl_multi_info_read(m, &left)) {
      if (msg->msg == CURLMSG_DONE && msg->easy_handle == e) {
        result = msg->data.result;
        done = true;
      }
    }
    if (done || running == 0) break;
    if (abort && abort()) {
      r.aborted = true;
      r.error = "aborted";
      break;
    }
    curl_multi_poll(m, nullptr, 0, 50, nullptr);
  }
  curl_multi_remove_handle(m, e);
  if (done && result == CURLE_OK) {
    curl_easy_getinfo(e, CURLINFO_RESPONSE_CODE, &r.status);
  } else if (done) {
    r.error = curl_easy_strerror(result);
  } else if (r.error.empty()) {
    r.error = "request did not finish";
  }
  return r;
}

HttpResponse Http::post_multipart(
    const std::string& url, const std::string& token,
    const std::vector<std::pair<std::string, std::string>>& fields, const FilePart& file,
    long timeout_ms, const Abort& abort) {
  CURL* e = curl_easy_init();
  if (!e) return HttpResponse{0, false, {}, "curl_easy_init failed"};
  std::string body;
  curl_slist* headers = auth_headers(token, false);
  common_options(e, url, timeout_ms, &body, headers);
  curl_mime* mime = curl_mime_init(e);
  curl_mimepart* part = curl_mime_addpart(mime);
  curl_mime_name(part, file.field.c_str());
  curl_mime_filename(part, file.filename.c_str());
  curl_mime_type(part, file.mime.c_str());
  curl_mime_data(part, file.data.data(), file.data.size());
  for (const auto& [name, value] : fields) {
    part = curl_mime_addpart(mime);
    curl_mime_name(part, name.c_str());
    curl_mime_data(part, value.data(), value.size());
  }
  curl_easy_setopt(e, CURLOPT_MIMEPOST, mime);
  HttpResponse r = perform(e, abort);
  r.body = std::move(body);
  curl_easy_cleanup(e);
  curl_mime_free(mime);
  curl_slist_free_all(headers);
  return r;
}

HttpResponse Http::post_json(const std::string& url, const std::string& token,
                             const std::string& body_in, long timeout_ms,
                             const Abort& abort) {
  CURL* e = curl_easy_init();
  if (!e) return HttpResponse{0, false, {}, "curl_easy_init failed"};
  std::string body;
  curl_slist* headers = auth_headers(token, !body_in.empty());
  common_options(e, url, timeout_ms, &body, headers);
  curl_easy_setopt(e, CURLOPT_POST, 1L);
  curl_easy_setopt(e, CURLOPT_POSTFIELDS, body_in.data());
  curl_easy_setopt(e, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(body_in.size()));
  HttpResponse r = perform(e, abort);
  r.body = std::move(body);
  curl_easy_cleanup(e);
  curl_slist_free_all(headers);
  return r;
}

std::string join_url(const std::string& base, const std::string& path) {
  std::string b = base;
  while (!b.empty() && b.back() == '/') b.pop_back();
  return b + path;
}

}  // namespace athena
