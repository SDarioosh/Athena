#include "athena/core.h"

int main() {
  athena::CoreConfig cfg;
  cfg.upload_url = "http://127.0.0.1:9";
  cfg.session_id = "demo-session";
  athena::Core core(cfg);
  return core.stop_requested() ? 1 : 0;
}
