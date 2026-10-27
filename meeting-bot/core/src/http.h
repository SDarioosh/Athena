// Minimal libcurl client for the uploads and the end-of-meeting calls. Every request can
// be cut short by an abort check polled every 50 ms, so finish() keeps its deadline even
// against a server that never answers. Use from one thread at a time.
#pragma once

#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace athena {

struct HttpResponse {
  long status = 0;       // 0: no HTTP answer (network error, timeout or aborted)
  bool aborted = false;  // the abort check said stop
  std::string body;
  std::string error;
  bool json_true(const char* key) const;  // body has "key": true
};

class Http {
 public:
  using Abort = std::function<bool()>;
  struct FilePart {
    std::string field, filename, mime, data;
  };

  Http();
  ~Http();
  Http(const Http&) = delete;
  Http& operator=(const Http&) = delete;

  HttpResponse post_multipart(const std::string& url, const std::string& token,
                              const std::vector<std::pair<std::string, std::string>>& fields,
                              const FilePart& file, long timeout_ms, const Abort& abort);
  // body empty: a POST without a body
  HttpResponse post_json(const std::string& url, const std::string& token,
                         const std::string& body, long timeout_ms, const Abort& abort);

 private:
  HttpResponse perform(void* easy, const Abort& abort);
  void* multi_ = nullptr;
};

// "http://host:port" + "/v1/..." without a doubled slash.
std::string join_url(const std::string& base, const std::string& path);

}  // namespace athena
