#pragma once

#include <cstdint>
#include <string>
#include <vector>

// Line-oriented TCP control channel bound to 127.0.0.1. One client at a time.
// Polled from the render loop without blocking; commands run on the main thread.
class ControlServer {
public:
    ControlServer() = default;
    ~ControlServer();
    ControlServer(const ControlServer&) = delete;
    ControlServer& operator=(const ControlServer&) = delete;

    bool start(uint16_t port, std::string* err);
    void stop();
    bool active() const { return listen_fd_ != kInvalid; }
    bool has_client() const { return client_fd_ != kInvalid; }

    // Accepts a pending client and returns complete lines received since the last poll.
    std::vector<std::string> poll();
    // Sends one line (newline appended). Drops the client on failure.
    void send_line(const std::string& line);

private:
    using Socket = intptr_t;
    static constexpr Socket kInvalid = -1;

    void drop_client();

    Socket listen_fd_ = kInvalid;
    Socket client_fd_ = kInvalid;
    std::string buffer_;
    bool wsa_started_ = false;
};
