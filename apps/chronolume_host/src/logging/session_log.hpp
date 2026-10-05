#pragma once

#include <map>
#include <string>

class SessionLog {
public:
    bool start(const std::string& path);
    void stop();
    bool active() const { return active_; }
    const std::string& path() const { return path_; }

    void event(const std::string& type, const std::map<std::string, std::string>& fields);

private:
    bool active_ = false;
    std::string path_;
    void* file_ = nullptr;
};
