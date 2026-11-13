#include "line_client.h"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace av {

LineClient::LineClient(std::string path, size_t max_line, size_t max_out)
    : path_(std::move(path)), max_line_(max_line), max_out_(max_out) {}

LineClient::~LineClient() { close(); }

void LineClient::close() {
    if (fd_ >= 0) ::close(fd_);
    fd_ = -1;
    in_.clear();
    out_.clear();
    discarding_ = false;
}

bool LineClient::connect_once(std::string *err) {
    if (fd_ >= 0) return true;
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (path_.empty() || path_.size() >= sizeof(addr.sun_path)) {
        if (err) *err = "bad socket path";
        return false;
    }
    std::memcpy(addr.sun_path, path_.c_str(), path_.size() + 1);
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        if (err) *err = std::strerror(errno);
        return false;
    }
    // A blocking connect on a local socket returns at once (or fails at once).
    if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
        if (err) *err = std::strerror(errno);
        ::close(fd);
        return false;
    }
    int fl = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    fd_ = fd;
    in_.clear();
    out_.clear();
    discarding_ = false;
    return true;
}

bool LineClient::read_lines(std::vector<std::string> &out) {
    if (fd_ < 0) return false;
    char buf[2048];
    while (true) {
        ssize_t n = ::recv(fd_, buf, sizeof(buf), 0);
        if (n == 0) {
            close();
            return false;
        }
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
            close();
            return false;
        }
        for (ssize_t i = 0; i < n; i++) {
            char c = buf[i];
            if (c == '\n') {
                if (!discarding_) out.push_back(in_);
                in_.clear();
                discarding_ = false;
                continue;
            }
            if (discarding_) continue;
            if (in_.size() + 2 > max_line_) {  // the line with its '\n' would be too long
                in_.clear();
                discarding_ = true;
                continue;
            }
            in_ += c;
        }
    }
}

bool LineClient::send(const std::string &line) {
    if (fd_ < 0 || line.empty() || out_.size() + line.size() > max_out_) {
        dropped_++;
        return false;
    }
    out_ += line;
    return flush();
}

bool LineClient::flush() {
    if (fd_ < 0) return false;
    while (!out_.empty()) {
        ssize_t n = ::send(fd_, out_.data(), out_.size(), MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
            close();
            return false;
        }
        out_.erase(0, static_cast<size_t>(n));
    }
    return true;
}

}  // namespace av
