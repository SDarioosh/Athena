// The socket client against a real Unix socket server in a temp directory: lines both
// ways, split reads, overlong lines, a full send buffer, the server going away.
#include <cstdlib>
#include <cstring>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "line_client.h"
#include "testlib.h"

namespace {

struct Server {
    std::string dir, path;
    int lfd = -1, cfd = -1;
    Server() {
        char tmpl[] = "/tmp/athena_vision_sock_XXXXXX";
        char *d = mkdtemp(tmpl);
        dir = d ? d : "/tmp";
        path = dir + "/vision.sock";
        lfd = socket(AF_UNIX, SOCK_STREAM, 0);
        sockaddr_un a{};
        a.sun_family = AF_UNIX;
        std::strncpy(a.sun_path, path.c_str(), sizeof(a.sun_path) - 1);
        CHECK(bind(lfd, reinterpret_cast<sockaddr *>(&a), sizeof(a)) == 0);
        CHECK(listen(lfd, 1) == 0);
    }
    void accept_one() { cfd = accept(lfd, nullptr, nullptr); }
    void send_raw(const std::string &s) { CHECK(write(cfd, s.data(), s.size()) == ssize_t(s.size())); }
    std::string read_some(int timeout_ms = 500) {
        pollfd p{cfd, POLLIN, 0};
        std::string out;
        while (poll(&p, 1, timeout_ms) > 0) {
            char buf[65536];
            ssize_t n = read(cfd, buf, sizeof(buf));
            if (n <= 0) break;
            out.append(buf, static_cast<size_t>(n));
            timeout_ms = 50;
        }
        return out;
    }
    void drop_client() {
        if (cfd >= 0) close(cfd);
        cfd = -1;
    }
    ~Server() {
        drop_client();
        if (lfd >= 0) close(lfd);
        unlink(path.c_str());
        rmdir(dir.c_str());
    }
};

void read_until(av::LineClient &c, std::vector<std::string> &lines, size_t n) {
    for (int i = 0; i < 100 && lines.size() < n; i++) {
        pollfd p{c.fd(), POLLIN, 0};
        poll(&p, 1, 20);
        if (!c.read_lines(lines)) break;
    }
}

}  // namespace

TEST(no_server_no_connection) {
    av::LineClient c("/tmp/athena_vision_no_such_dir/vision.sock");
    std::string err;
    CHECK(!c.connect_once(&err));
    CHECK(!err.empty());
    CHECK(!c.connected());
    CHECK(!c.send("x\n"));
    std::vector<std::string> l;
    CHECK(!c.read_lines(l));
    av::LineClient bad(std::string(200, 'x'));
    CHECK(!bad.connect_once(&err));
}

TEST(lines_both_ways) {
    Server s;
    av::LineClient c(s.path);
    CHECK(c.connect_once());
    s.accept_one();
    CHECK(c.send("{\"t\":\"state\"}\n"));
    CHECK(c.send("{\"t\":\"hand\"}\n"));
    CHECK_EQ(s.read_some(), std::string("{\"t\":\"state\"}\n{\"t\":\"hand\"}\n"));
    s.send_raw("{\"t\":\"mo");
    std::vector<std::string> lines;
    c.read_lines(lines);
    CHECK(lines.empty());  // half a line
    s.send_raw("de\"}\n{\"a\":1}\n");
    read_until(c, lines, 2);
    CHECK_EQ(lines.size(), size_t(2));
    CHECK_EQ(lines[0], std::string("{\"t\":\"mode\"}"));
}

TEST(overlong_lines_dropped_whole) {
    Server s;
    av::LineClient c(s.path, 1024);
    CHECK(c.connect_once());
    s.accept_one();
    std::string ok(1023, 'a');   // 1023 + '\n' = 1024: fits
    std::string big(1024, 'b');  // 1025 with '\n': dropped
    s.send_raw(ok + "\n" + big + "\n" + std::string(5000, 'c') + "\nlast\n");
    std::vector<std::string> lines;
    read_until(c, lines, 2);
    CHECK_EQ(lines.size(), size_t(2));
    CHECK(lines.size() == 2 && lines[0] == ok && lines[1] == "last");
}

TEST(full_buffer_drops_instead_of_blocking) {
    Server s;
    av::LineClient c(s.path, 1024, 4096);
    CHECK(c.connect_once());
    s.accept_one();
    std::string line(500, 'h');
    line += '\n';
    int sent = 0;
    for (int i = 0; i < 5000; i++) sent += c.send(line);  // nobody reads
    CHECK(c.dropped() > 0);
    CHECK(sent > 0);
    CHECK(c.connected());
    std::string got = s.read_some();
    CHECK(!got.empty());
    CHECK(got.size() % line.size() == 0);  // only whole lines
    while (c.want_write()) {
        c.flush();
        got += s.read_some(50);
    }
    CHECK(got.size() % line.size() == 0);
}

TEST(server_goes_away) {
    Server s;
    av::LineClient c(s.path);
    CHECK(c.connect_once());
    s.accept_one();
    s.drop_client();
    std::vector<std::string> lines;
    bool alive = true;
    for (int i = 0; i < 50 && alive; i++) {
        pollfd p{c.fd(), POLLIN, 0};
        poll(&p, 1, 20);
        alive = c.read_lines(lines);
    }
    CHECK(!alive);
    CHECK(!c.connected());
    // and reconnects when it is back
    CHECK(c.connect_once());
    s.accept_one();
    CHECK(c.send("x\n"));
    CHECK_EQ(s.read_some(), std::string("x\n"));
}

TEST_MAIN()
