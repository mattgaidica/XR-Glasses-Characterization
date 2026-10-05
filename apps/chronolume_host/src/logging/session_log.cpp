#include "logging/session_log.hpp"

#include <chrono>
#include <cstdio>
#include <ctime>

namespace {

std::string json_escape(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        switch (c) {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\n':
                out += "\\n";
                break;
            default:
                out += c;
                break;
        }
    }
    return out;
}

std::string iso_now() {
    using clock = std::chrono::system_clock;
    const auto now = clock::now();
    const std::time_t t = clock::to_time_t(now);
    std::tm tm{};
#if defined(_WIN32)
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        now.time_since_epoch())
                        .count()
                    % 1000;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d.%03d",
                  tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour,
                  tm.tm_min, tm.tm_sec, static_cast<int>(ms));
    return buf;
}

}  // namespace

bool SessionLog::start(const std::string& path) {
    stop();
    FILE* f = std::fopen(path.c_str(), "w");
    if (!f) {
        return false;
    }
    file_ = f;
    path_ = path;
    active_ = true;
    event("session_start", {});
    return true;
}

void SessionLog::stop() {
    if (!file_) {
        active_ = false;
        return;
    }
    if (active_) {
        event("session_stop", {});
    }
    std::fclose(static_cast<FILE*>(file_));
    file_ = nullptr;
    active_ = false;
}

void SessionLog::event(const std::string& type,
                       const std::map<std::string, std::string>& fields) {
    FILE* f = static_cast<FILE*>(file_);
    if (!f) {
        return;
    }
    std::fprintf(f, "{\"ts\":\"%s\",\"type\":\"%s\"", json_escape(iso_now()).c_str(),
                 json_escape(type).c_str());
    for (const auto& kv : fields) {
        std::fprintf(f, ",\"%s\":\"%s\"", json_escape(kv.first).c_str(),
                     json_escape(kv.second).c_str());
    }
    std::fprintf(f, "}\n");
    std::fflush(f);
}
