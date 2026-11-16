// The client end of a Unix stream socket carrying '\n'-terminated lines (vision.sock).
// Non-blocking: the device loop polls fd() and calls read_lines() / flush().
//
// Incoming lines longer than max_line are dropped whole. Outgoing lines wait in a bounded
// buffer; when it is full new lines are dropped (counted in dropped()) so a stalled UI can
// never block the camera loop.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace av {

class LineClient {
public:
    explicit LineClient(std::string path, size_t max_line = 1024, size_t max_out = 16 * 1024);
    ~LineClient();
    LineClient(const LineClient &) = delete;
    LineClient &operator=(const LineClient &) = delete;

    // One connection attempt; true when connected.
    bool connect_once(std::string *err = nullptr);
    void close();
    bool connected() const { return fd_ >= 0; }
    int fd() const { return fd_; }

    // Reads what is there. False when the peer closed the connection or it failed (the
    // client is then closed).
    bool read_lines(std::vector<std::string> &out);
    // Queues one line (must end with '\n'). False if it was dropped.
    bool send(const std::string &line);
    // Writes what the socket takes now. False on a broken connection (then closed).
    bool flush();
    bool want_write() const { return !out_.empty(); }
    size_t dropped() const { return dropped_; }

private:
    std::string path_;
    size_t max_line_, max_out_;
    int fd_ = -1;
    std::string in_;
    bool discarding_ = false;  // inside an overlong line
    std::string out_;
    size_t dropped_ = 0;
};

}  // namespace av
