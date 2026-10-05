#include "control/control_server.hpp"

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#include <cerrno>
#include <cstdio>
#include <cstring>

namespace {

#if defined(_WIN32)
using NativeSocket = SOCKET;
constexpr NativeSocket kNativeInvalid = INVALID_SOCKET;

void close_socket(NativeSocket s) { closesocket(s); }

bool set_nonblocking(NativeSocket s) {
    u_long mode = 1;
    return ioctlsocket(s, FIONBIO, &mode) == 0;
}

bool would_block() { return WSAGetLastError() == WSAEWOULDBLOCK; }

std::string socket_error() { return "socket error " + std::to_string(WSAGetLastError()); }
#else
using NativeSocket = int;
constexpr NativeSocket kNativeInvalid = -1;

void close_socket(NativeSocket s) { ::close(s); }

bool set_nonblocking(NativeSocket s) {
    const int flags = fcntl(s, F_GETFL, 0);
    return flags >= 0 && fcntl(s, F_SETFL, flags | O_NONBLOCK) == 0;
}

bool would_block() { return errno == EAGAIN || errno == EWOULDBLOCK; }

std::string socket_error() { return std::strerror(errno); }
#endif

#if defined(MSG_NOSIGNAL)
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif

void suppress_sigpipe(NativeSocket s) {
#if defined(SO_NOSIGPIPE)
    int yes = 1;
    setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
#else
    (void)s;
#endif
}

NativeSocket native(intptr_t s) { return static_cast<NativeSocket>(s); }

}  // namespace

ControlServer::~ControlServer() { stop(); }

bool ControlServer::start(uint16_t port, std::string* err) {
    stop();
#if defined(_WIN32)
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        *err = "WSAStartup failed";
        return false;
    }
    wsa_started_ = true;
#endif
    NativeSocket s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == kNativeInvalid) {
        *err = socket_error();
        stop();
        return false;
    }
    int yes = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 || listen(s, 1) != 0 ||
        !set_nonblocking(s)) {
        *err = "bind/listen 127.0.0.1:" + std::to_string(port) + ": " + socket_error();
        close_socket(s);
        stop();
        return false;
    }
    listen_fd_ = static_cast<Socket>(s);
    return true;
}

void ControlServer::stop() {
    drop_client();
    if (listen_fd_ != kInvalid) {
        close_socket(native(listen_fd_));
        listen_fd_ = kInvalid;
    }
#if defined(_WIN32)
    if (wsa_started_) {
        WSACleanup();
        wsa_started_ = false;
    }
#endif
}

void ControlServer::drop_client() {
    if (client_fd_ != kInvalid) {
        close_socket(native(client_fd_));
        client_fd_ = kInvalid;
        std::puts("control client disconnected");
    }
    buffer_.clear();
}

std::vector<std::string> ControlServer::poll() {
    std::vector<std::string> lines;
    if (listen_fd_ == kInvalid) {
        return lines;
    }

    NativeSocket incoming = accept(native(listen_fd_), nullptr, nullptr);
    if (incoming != kNativeInvalid) {
        if (client_fd_ != kInvalid) {
            static const char kBusy[] = "{\"ok\":false,\"error\":\"busy: another client is connected\"}\n";
            suppress_sigpipe(incoming);
            send(incoming, kBusy, static_cast<int>(sizeof(kBusy) - 1), kSendFlags);
            close_socket(incoming);
        } else if (set_nonblocking(incoming)) {
            suppress_sigpipe(incoming);
            int yes = 1;
            setsockopt(incoming, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&yes),
                       sizeof(yes));
            client_fd_ = static_cast<Socket>(incoming);
            buffer_.clear();
            std::puts("control client connected");
        } else {
            close_socket(incoming);
        }
    }

    if (client_fd_ == kInvalid) {
        return lines;
    }

    char chunk[1024];
    for (;;) {
        const auto n = recv(native(client_fd_), chunk, static_cast<int>(sizeof(chunk)), 0);
        if (n > 0) {
            buffer_.append(chunk, static_cast<size_t>(n));
            continue;
        }
        if (n == 0 || !would_block()) {
            drop_client();
        }
        break;
    }

    size_t start = 0;
    for (size_t nl = buffer_.find('\n', start); nl != std::string::npos;
         nl = buffer_.find('\n', start)) {
        std::string line = buffer_.substr(start, nl - start);
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            lines.push_back(line);
        }
        start = nl + 1;
    }
    buffer_.erase(0, start);
    return lines;
}

void ControlServer::send_line(const std::string& line) {
    if (client_fd_ == kInvalid) {
        return;
    }
    const std::string out = line + "\n";
    size_t sent = 0;
    while (sent < out.size()) {
        const auto n = send(native(client_fd_), out.data() + sent,
                            static_cast<int>(out.size() - sent), kSendFlags);
        if (n > 0) {
            sent += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && would_block()) {
            continue;
        }
        drop_client();
        return;
    }
}
