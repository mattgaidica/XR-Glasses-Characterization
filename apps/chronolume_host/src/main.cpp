#include "control/control_server.hpp"
#include "device/device.hpp"
#include "display/frame_image.hpp"
#include "display/macos_colorspace.h"
#include "display/stimulus.hpp"
#include "logging/session_log.hpp"

#include <GLFW/glfw3.h>

#ifdef __APPLE__
#include <OpenGL/gl.h>
#else
#include <GL/gl.h>
#endif

#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr uint8_t kBlueLadder[] = {0, 16, 32, 64, 128, 192, 255};

struct App {
    std::unique_ptr<Device> device;
    StimulusState stimulus;
    SessionLog log;
    std::string calibration_version = "uncalibrated";
    bool dirty = true;
    ControlServer control;
    std::string remote_label;
    int monitor_index = -1;
    uint64_t frame = 0;
    int last_wear = -2;
    FrameImage image;
    unsigned int image_tex = 0;
};

void print_keys() {
    std::puts("Chronolume host — terminal is the operator console.");
    std::puts("  0-6     blue ladder 0, 16, 32, 64, 128, 192, 255");
    std::puts("  K R G B W   black / red / green / blue255 / white");
    std::puts("  F1 F2 F3    left / right / both eyes (SBS halves)");
    std::puts("  [ ]     brightness down / up");
    std::puts("  , .     duty cycle down / up by 10 (0-100)");
    std::puts("  9       duty cycle 98 (SDK H preset)");
    std::puts("  F       toggle electrochromic film");
    std::puts("  S       start/stop session log");
    std::puts("  D       capability dump");
    std::puts("  V       read back framebuffer RGB (each half)");
    std::puts("  P       fullscreen");
    std::puts("  Esc     quit");
}

void print_info(const DeviceInfo& info) {
    std::printf("backend      %s\n", info.backend.c_str());
    std::printf("name         %s\n", info.market_name.c_str());
    std::printf("product_id   0x%04X\n", info.product_id);
    std::printf("device_type  %d\n", info.device_type);
    std::printf("firmware     %s\n", info.firmware.c_str());
    std::printf("display_mode 0x%02X\n", info.display_mode);
    std::printf("brightness   %d\n", info.brightness);
    std::printf("duty_cycle   %d\n", info.duty_cycle);
    std::printf("film         %.3f\n", info.film);
    std::printf("wear         %d\n", info.wear_status);
    std::printf("connected    %s\n", info.connected ? "yes" : "no");
    if (!info.last_error.empty()) {
        std::printf("error        %s\n", info.last_error.c_str());
    }
}

std::map<std::string, std::string> stimulus_fields(const App& app) {
    const DeviceInfo info = app.device->info();
    return {
        {"r", std::to_string(app.stimulus.r)},
        {"g", std::to_string(app.stimulus.g)},
        {"b", std::to_string(app.stimulus.b)},
        {"label", app.stimulus.label},
        {"eye", eye_name(app.stimulus.eye)},
        {"brightness", std::to_string(info.brightness)},
        {"duty_cycle", std::to_string(info.duty_cycle)},
        {"film", std::to_string(info.film)},
        {"display_mode", std::to_string(info.display_mode)},
        {"wear", std::to_string(info.wear_status)},
        {"calibration", app.calibration_version},
        {"backend", info.backend},
        {"device", info.market_name},
        {"image", app.stimulus.image ? app.image.path : ""},
        {"image_hash", app.stimulus.image ? hex64(app.image.hash) : ""},
    };
}

std::string stimulus_desc(const App& app) {
    if (app.stimulus.image) {
        return "image " + std::to_string(app.image.w) + "x" + std::to_string(app.image.h) + " " +
               hex64(app.image.hash);
    }
    return rgb_string(app.stimulus);
}

void log_stimulus(App* app) {
    if (app->log.active()) {
        app->log.event("stimulus", stimulus_fields(*app));
    }
    const DeviceInfo info = app->device->info();
    std::printf("stimulus %s %s  eye=%s  bri=%d duty=%d film=%.0f\n",
                app->stimulus.label, stimulus_desc(*app).c_str(),
                eye_name(app->stimulus.eye), info.brightness, info.duty_cycle,
                info.film);
}

void draw_half(float x0, float x1, uint8_t r, uint8_t g, uint8_t b) {
    const GLubyte px[3] = {r, g, b};
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB8, 1, 1, 0, GL_RGB, GL_UNSIGNED_BYTE, px);
    glBegin(GL_QUADS);
    glTexCoord2f(0, 0);
    glVertex2f(x0, -1);
    glTexCoord2f(1, 0);
    glVertex2f(x1, -1);
    glTexCoord2f(1, 1);
    glVertex2f(x1, 1);
    glTexCoord2f(0, 1);
    glVertex2f(x0, 1);
    glEnd();
}

void set_nearest_clamp() {
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

// The image's top row is texture row 0, so t runs from 1 at the bottom edge to 0 at the top.
void draw_image_half(float x0, float x1, float s0, float s1) {
    glBegin(GL_QUADS);
    glTexCoord2f(s0, 1);
    glVertex2f(x0, -1);
    glTexCoord2f(s1, 1);
    glVertex2f(x1, -1);
    glTexCoord2f(s1, 0);
    glVertex2f(x1, 1);
    glTexCoord2f(s0, 0);
    glVertex2f(x0, 1);
    glEnd();
}

void render_stimulus(const StimulusState& s, unsigned int color_tex, unsigned int image_tex) {
    glDisable(GL_BLEND);
    glDisable(GL_DEPTH_TEST);
    glEnable(GL_TEXTURE_2D);

    const bool left_on = s.eye == EyeTarget::Left || s.eye == EyeTarget::Both;
    const bool right_on = s.eye == EyeTarget::Right || s.eye == EyeTarget::Both;
    if (s.image) {
        glBindTexture(GL_TEXTURE_2D, image_tex);
        set_nearest_clamp();
        if (left_on) {
            draw_image_half(-1.0f, 0.0f, 0.0f, 0.5f);
        }
        if (right_on) {
            draw_image_half(0.0f, 1.0f, 0.5f, 1.0f);
        }
        glBindTexture(GL_TEXTURE_2D, color_tex);
        set_nearest_clamp();
        if (!left_on) {
            draw_half(-1.0f, 0.0f, 0, 0, 0);
        }
        if (!right_on) {
            draw_half(0.0f, 1.0f, 0, 0, 0);
        }
        return;
    }
    glBindTexture(GL_TEXTURE_2D, color_tex);
    set_nearest_clamp();
    draw_half(-1.0f, 0.0f, left_on ? s.r : 0, left_on ? s.g : 0, left_on ? s.b : 0);
    draw_half(0.0f, 1.0f, right_on ? s.r : 0, right_on ? s.g : 0, right_on ? s.b : 0);
}

void readback_halves(GLFWwindow* window) {
    int w = 0, h = 0;
    glfwGetFramebufferSize(window, &w, &h);
    if (w < 2 || h < 1) {
        return;
    }
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    GLubyte left[3] = {};
    GLubyte right[3] = {};
    glReadPixels(w / 4, h / 2, 1, 1, GL_RGB, GL_UNSIGNED_BYTE, left);
    glReadPixels((3 * w) / 4, h / 2, 1, 1, GL_RGB, GL_UNSIGNED_BYTE, right);
    std::printf("readback fb=%dx%d  left=(%u,%u,%u)  right=(%u,%u,%u)\n", w, h,
                left[0], left[1], left[2], right[0], right[1], right[2]);
}

std::string default_log_path() {
    const std::filesystem::path dir =
        std::filesystem::path(CHRONOLUME_ROOT) / "data" / "sessions";
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    using clock = std::chrono::system_clock;
    const std::time_t t = clock::to_time_t(clock::now());
    std::tm tm{};
#if defined(_WIN32)
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char name[64];
    std::snprintf(name, sizeof(name), "session_%04d%02d%02d_%02d%02d%02d.jsonl",
                  tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour,
                  tm.tm_min, tm.tm_sec);
    return (dir / name).string();
}

void update_title(GLFWwindow* window, const App& app) {
    const DeviceInfo info = app.device->info();
    char title[256];
    std::snprintf(title, sizeof(title),
                  "Chronolume stimulus  %s %s  eye=%s  bri=%d duty=%d film=%.0f",
                  app.stimulus.label, stimulus_desc(app).c_str(),
                  eye_name(app.stimulus.eye), info.brightness, info.duty_cycle, info.film);
    glfwSetWindowTitle(window, title);
}

void set_duty_manual(App* app, int requested) {
    requested = requested < 0 ? 0 : (requested > 100 ? 100 : requested);
    const bool ok = app->device->set_duty_cycle(requested);
    const DeviceInfo info = app->device->info();
    std::printf("duty requested=%d reported=%d bri=%d%s%s\n", requested, info.duty_cycle,
                info.brightness, ok ? "" : "  FAILED: ", ok ? "" : info.last_error.c_str());
    if (app->log.active()) {
        std::map<std::string, std::string> fields = stimulus_fields(*app);
        fields["requested_duty_cycle"] = std::to_string(requested);
        fields["ok"] = ok ? "1" : "0";
        app->log.event("duty", fields);
    }
    app->dirty = true;
}

bool key_pressed(GLFWwindow* window, int key) {
    static bool latch[512] = {};
    if (key < 0 || key >= 512) {
        return false;
    }
    const bool down = glfwGetKey(window, key) == GLFW_PRESS;
    const bool edge = down && !latch[key];
    latch[key] = down;
    return edge;
}

void list_monitors() {
    int count = 0;
    GLFWmonitor** monitors = glfwGetMonitors(&count);
    GLFWmonitor* primary = glfwGetPrimaryMonitor();
    for (int i = 0; i < count; ++i) {
        const GLFWvidmode* mode = glfwGetVideoMode(monitors[i]);
        int x = 0, y = 0;
        glfwGetMonitorPos(monitors[i], &x, &y);
        std::printf("monitor %d  %s  %dx%d@%d  pos=(%d,%d)%s\n", i, glfwGetMonitorName(monitors[i]),
                    mode ? mode->width : 0, mode ? mode->height : 0,
                    mode ? mode->refreshRate : 0, x, y,
                    monitors[i] == primary ? "  primary" : "");
    }
}

GLFWmonitor* fullscreen_monitor(const App& app) {
    if (app.monitor_index >= 0) {
        int count = 0;
        GLFWmonitor** monitors = glfwGetMonitors(&count);
        if (app.monitor_index < count) {
            return monitors[app.monitor_index];
        }
    }
    return glfwGetPrimaryMonitor();
}

void set_fullscreen(GLFWwindow* window, const App& app, bool on) {
    if (!on) {
        glfwSetWindowMonitor(window, nullptr, 80, 80, 1920, 540, 0);
        return;
    }
    GLFWmonitor* monitor = fullscreen_monitor(app);
    const GLFWvidmode* mode = glfwGetVideoMode(monitor);
    glfwSetWindowMonitor(window, monitor, 0, 0, mode->width, mode->height, mode->refreshRate);
#ifdef __APPLE__
    chronolume_set_device_rgb_color_space(window);
#endif
}

std::string json_str(const std::string& in) {
    std::string out = "\"";
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
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out + "\"";
}

std::string json_object(const std::map<std::string, std::string>& fields) {
    std::string out = "{";
    bool first = true;
    for (const auto& kv : fields) {
        if (!first) {
            out += ",";
        }
        first = false;
        out += json_str(kv.first) + ":" + json_str(kv.second);
    }
    return out + "}";
}

struct Readback {
    int fb_w = 0;
    int fb_h = 0;
    GLubyte left[3] = {};
    GLubyte right[3] = {};
    // Image stimuli: the whole framebuffer compared with the image (unshown halves black).
    bool image = false;
    bool image_match = false;
    long long image_mismatched = -1;  // pixels differing; -1 when the sizes differ
    std::string fb_hash;
};

Readback read_back_buffer(GLFWwindow* window, const App& app) {
    Readback rb;
    glfwGetFramebufferSize(window, &rb.fb_w, &rb.fb_h);
    if (rb.fb_w < 2 || rb.fb_h < 1) {
        rb.image = app.stimulus.image;
        return rb;
    }
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadBuffer(GL_BACK);
    glReadPixels(rb.fb_w / 4, rb.fb_h / 2, 1, 1, GL_RGB, GL_UNSIGNED_BYTE, rb.left);
    glReadPixels((3 * rb.fb_w) / 4, rb.fb_h / 2, 1, 1, GL_RGB, GL_UNSIGNED_BYTE, rb.right);
    if (!app.stimulus.image) {
        return rb;
    }
    rb.image = true;
    const size_t w = static_cast<size_t>(rb.fb_w), h = static_cast<size_t>(rb.fb_h);
    std::vector<GLubyte> fb(w * h * 3);
    glReadPixels(0, 0, rb.fb_w, rb.fb_h, GL_RGB, GL_UNSIGNED_BYTE, fb.data());
    // glReadPixels returns the bottom row first; compare and hash top row first like the image.
    std::vector<GLubyte> top_first(fb.size());
    for (size_t y = 0; y < h; ++y) {
        std::memcpy(&top_first[y * w * 3], &fb[(h - 1 - y) * w * 3], w * 3);
    }
    rb.fb_hash = hex64(fnv1a64(top_first.data(), top_first.size()));
    if (app.image.w != rb.fb_w || app.image.h != rb.fb_h) {
        return rb;
    }
    const bool left_on = app.stimulus.eye == EyeTarget::Left || app.stimulus.eye == EyeTarget::Both;
    const bool right_on = app.stimulus.eye == EyeTarget::Right || app.stimulus.eye == EyeTarget::Both;
    const size_t half = w / 2;
    long long bad = 0;
    for (size_t y = 0; y < h; ++y) {
        for (size_t x = 0; x < w; ++x) {
            const bool on = x < half ? left_on : right_on;
            const size_t i = (y * w + x) * 3;
            for (int c = 0; c < 3; ++c) {
                const GLubyte want = on ? app.image.rgb[i + c] : 0;
                if (top_first[i + c] != want) {
                    ++bad;
                    break;
                }
            }
        }
    }
    rb.image_mismatched = bad;
    rb.image_match = bad == 0;
    return rb;
}

struct CommandResult {
    std::string cmd;
    bool ok = true;
    std::string error;
    std::map<std::string, std::string> extra;
    bool quit = false;
};

bool parse_int(const std::string& s, int lo, int hi, int* out) {
    if (s.empty()) {
        return false;
    }
    char* end = nullptr;
    const long v = std::strtol(s.c_str(), &end, 10);
    if (*end != '\0' || v < lo || v > hi) {
        return false;
    }
    *out = static_cast<int>(v);
    return true;
}

// The line after its first n whitespace-separated tokens, without surrounding whitespace.
std::string rest_after_tokens(const std::string& line, int n) {
    const char* ws = " \t\r\n";
    size_t pos = line.find_first_not_of(ws);
    for (int i = 0; i < n && pos != std::string::npos; ++i) {
        pos = line.find_first_of(ws, pos);
        pos = pos == std::string::npos ? pos : line.find_first_not_of(ws, pos);
    }
    if (pos == std::string::npos) {
        return "";
    }
    const size_t end = line.find_last_not_of(ws);
    return line.substr(pos, end - pos + 1);
}

CommandResult run_command(App* app, GLFWwindow* window, const std::string& line) {
    std::istringstream in(line);
    std::vector<std::string> tok;
    for (std::string t; in >> t;) {
        tok.push_back(t);
    }
    CommandResult res;
    if (tok.empty()) {
        res.ok = false;
        res.error = "empty command";
        return res;
    }
    res.cmd = tok[0];
    auto fail = [&res](const std::string& msg) {
        res.ok = false;
        res.error = msg;
    };
    if (app->log.active()) {
        app->log.event("control", {{"command", line}});
    }

    if (res.cmd == "ping") {
        res.extra["reply"] = "pong";
    } else if (res.cmd == "info") {
        const DeviceInfo info = app->device->info();
        res.extra["firmware"] = info.firmware;
        res.extra["product_id"] = std::to_string(info.product_id);
        res.extra["device_type"] = std::to_string(info.device_type);
        res.extra["connected"] = info.connected ? "1" : "0";
        res.extra["last_error"] = info.last_error;
        res.extra["fullscreen"] = glfwGetWindowMonitor(window) ? "1" : "0";
        res.extra["monitor_index"] = std::to_string(app->monitor_index);
        res.extra["log_path"] = app->log.active() ? app->log.path() : "";
    } else if (res.cmd == "rgb") {
        int r = 0, g = 0, b = 0;
        if (tok.size() < 4 || !parse_int(tok[1], 0, 255, &r) || !parse_int(tok[2], 0, 255, &g) ||
            !parse_int(tok[3], 0, 255, &b)) {
            fail("usage: rgb R G B [label]  (0-255)");
        } else {
            std::string label;
            for (size_t i = 4; i < tok.size(); ++i) {
                label += (i > 4 ? " " : "") + tok[i];
            }
            app->remote_label = label.empty() ? "remote" : label;
            stimulus_set_rgb(&app->stimulus, static_cast<uint8_t>(r), static_cast<uint8_t>(g),
                             static_cast<uint8_t>(b), app->remote_label.c_str());
            app->dirty = true;
        }
    } else if (res.cmd == "image") {
        // The path is the rest of the line after the label, so it may contain spaces.
        const std::string path = tok.size() >= 3 ? rest_after_tokens(line, 2) : "";
        if (path.empty()) {
            fail("usage: image <label> <path.ppm>  (binary PPM, 8-bit RGB)");
        } else {
            FrameImage img;
            std::string err;
            if (!load_ppm(path, &img, &err)) {
                fail(err);
            } else {
                const std::string label = tok[1];
                app->image = std::move(img);
                glBindTexture(GL_TEXTURE_2D, app->image_tex);
                glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB8, app->image.w, app->image.h, 0, GL_RGB,
                             GL_UNSIGNED_BYTE, app->image.rgb.data());
                app->remote_label = label;
                stimulus_set_rgb(&app->stimulus, 0, 0, 0, app->remote_label.c_str());
                app->stimulus.image = true;
                res.extra["image_w"] = std::to_string(app->image.w);
                res.extra["image_h"] = std::to_string(app->image.h);
                res.extra["image_hash"] = hex64(app->image.hash);
                app->dirty = true;
            }
        }
    } else if (res.cmd == "eye") {
        const std::string e = tok.size() > 1 ? tok[1] : "";
        if (e == "left") {
            app->stimulus.eye = EyeTarget::Left;
        } else if (e == "right") {
            app->stimulus.eye = EyeTarget::Right;
        } else if (e == "both") {
            app->stimulus.eye = EyeTarget::Both;
        } else {
            fail("usage: eye left|right|both");
        }
        app->dirty = app->dirty || res.ok;
    } else if (res.cmd == "brightness") {
        int level = 0;
        if (tok.size() < 2 || !parse_int(tok[1], 0, 255, &level)) {
            fail("usage: brightness N");
        } else if (!app->device->set_brightness(level)) {
            fail("set_brightness failed: " + app->device->info().last_error);
        }
        app->dirty = true;
    } else if (res.cmd == "duty") {
        int percent = 0;
        if (tok.size() < 2 || !parse_int(tok[1], 0, 100, &percent)) {
            fail("usage: duty N (0-100)");
        } else if (!app->device->set_duty_cycle(percent)) {
            fail("set_duty_cycle failed: " + app->device->info().last_error);
        }
        app->dirty = true;
    } else if (res.cmd == "film") {
        const std::string v = tok.size() > 1 ? tok[1] : "";
        if (v != "on" && v != "off" && v != "1" && v != "0") {
            fail("usage: film on|off");
        } else if (!app->device->set_film((v == "on" || v == "1") ? 1.0f : 0.0f)) {
            fail("set_film failed: " + app->device->info().last_error);
        }
        app->dirty = true;
    } else if (res.cmd == "calibration") {
        if (tok.size() < 2) {
            fail("usage: calibration <version>");
        } else {
            app->calibration_version = tok[1];
            app->dirty = true;
        }
    } else if (res.cmd == "fullscreen") {
        const std::string v = tok.size() > 1 ? tok[1] : "";
        if (v != "on" && v != "off") {
            fail("usage: fullscreen on|off");
        } else {
            set_fullscreen(window, *app, v == "on");
        }
    } else if (res.cmd == "log") {
        const std::string v = tok.size() > 1 ? tok[1] : "";
        if (v == "start") {
            if (app->log.active()) {
                res.extra["log_path"] = app->log.path();
            } else {
                const std::string p = tok.size() > 2 ? tok[2] : default_log_path();
                if (app->log.start(p)) {
                    const DeviceInfo info = app->device->info();
                    app->log.event("capability", {
                        {"backend", info.backend},
                        {"device", info.market_name},
                        {"firmware", info.firmware},
                        {"product_id", std::to_string(info.product_id)},
                    });
                    log_stimulus(app);
                    std::printf("session started %s\n", p.c_str());
                    res.extra["log_path"] = p;
                } else {
                    fail("could not write " + p);
                }
            }
        } else if (v == "stop") {
            if (app->log.active()) {
                res.extra["log_path"] = app->log.path();
                app->log.stop();
                std::printf("session stopped %s\n", res.extra["log_path"].c_str());
            }
        } else {
            fail("usage: log start [path] | log stop");
        }
    } else if (res.cmd == "mark") {
        std::map<std::string, std::string> fields = stimulus_fields(*app);
        for (size_t i = 1; i < tok.size(); ++i) {
            const size_t eq = tok[i].find('=');
            if (eq == std::string::npos || eq == 0) {
                fields["mark_" + std::to_string(i)] = tok[i];
            } else {
                fields["mark." + tok[i].substr(0, eq)] = tok[i].substr(eq + 1);
            }
        }
        if (app->log.active()) {
            app->log.event("mark", fields);
        } else {
            fail("no active session log");
        }
    } else if (res.cmd == "quit") {
        res.quit = true;
    } else {
        fail("unknown command '" + res.cmd +
             "' (ping info rgb image eye brightness duty film calibration fullscreen log mark quit)");
    }
    return res;
}

std::string command_response(const App& app, const CommandResult& res, const Readback& rb) {
    char buf[160];
    std::snprintf(buf, sizeof(buf),
                  "{\"fb_w\":%d,\"fb_h\":%d,\"left\":[%u,%u,%u],\"right\":[%u,%u,%u]", rb.fb_w,
                  rb.fb_h, rb.left[0], rb.left[1], rb.left[2], rb.right[0], rb.right[1],
                  rb.right[2]);
    std::string readback = buf;
    if (rb.image) {
        readback += ",\"image\":{\"w\":" + std::to_string(app.image.w) +
                    ",\"h\":" + std::to_string(app.image.h) +
                    ",\"hash\":" + json_str(hex64(app.image.hash)) +
                    ",\"fb_hash\":" + json_str(rb.fb_hash) +
                    ",\"mismatched_pixels\":" + std::to_string(rb.image_mismatched) +
                    ",\"match\":" + (rb.image_match ? "true" : "false") + "}";
    }
    readback += "}";
    std::string out = "{\"ok\":" + std::string(res.ok ? "true" : "false");
    out += ",\"cmd\":" + json_str(res.cmd);
    if (!res.ok) {
        out += ",\"error\":" + json_str(res.error);
    }
    out += ",\"frame\":" + std::to_string(app.frame);
    out += ",\"state\":" + json_object(stimulus_fields(app));
    out += ",\"readback\":" + readback;
    if (!res.extra.empty()) {
        out += ",\"extra\":" + json_object(res.extra);
    }
    return out + "}";
}

}  // namespace

int main(int argc, char** argv) {
    std::string backend = "mock";
    int control_port = 0;
    int monitor_index = -1;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--device") == 0 && i + 1 < argc) {
            backend = argv[++i];
        } else if (std::strcmp(argv[i], "--control-port") == 0 && i + 1 < argc) {
            if (!parse_int(argv[++i], 1, 65535, &control_port)) {
                std::fprintf(stderr, "--control-port must be 1-65535\n");
                return 1;
            }
        } else if (std::strcmp(argv[i], "--monitor") == 0 && i + 1 < argc) {
            if (!parse_int(argv[++i], 0, 63, &monitor_index)) {
                std::fprintf(stderr, "--monitor must be a monitor index (see startup list)\n");
                return 1;
            }
        } else if (std::strcmp(argv[i], "--help") == 0 || std::strcmp(argv[i], "-h") == 0) {
            std::puts("usage: chronolume_host [--device mock|viture] [--control-port N] [--monitor N]");
            return 0;
        }
    }

    App app;
    app.monitor_index = monitor_index;
    if (backend == "mock") {
        app.device = make_mock_device();
    } else if (backend == "viture") {
#ifdef CHRONOLUME_WITH_VITURE
        app.device = make_viture_device();
#else
        std::fprintf(stderr, "built without VITURE SDK\n");
        return 1;
#endif
    } else {
        std::fprintf(stderr, "unknown --device %s (use mock or viture)\n", backend.c_str());
        return 1;
    }

    if (!app.device->open()) {
        const DeviceInfo info = app.device->info();
        std::fprintf(stderr, "device open failed: %s\n", info.last_error.c_str());
        return 1;
    }

    print_keys();
    print_info(app.device->info());
    stimulus_set_rgb(&app.stimulus, 0, 0, 0, "black");

    if (!glfwInit()) {
        std::fprintf(stderr, "glfwInit failed\n");
        return 1;
    }
    list_monitors();
    if (control_port > 0) {
        std::string err;
        if (!app.control.start(static_cast<uint16_t>(control_port), &err)) {
            std::fprintf(stderr, "control server failed: %s\n", err.c_str());
            glfwTerminate();
            return 1;
        }
        std::printf("control listening on 127.0.0.1:%d\n", control_port);
    }
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 2);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 1);
    glfwWindowHint(GLFW_COCOA_RETINA_FRAMEBUFFER, GLFW_FALSE);
    glfwWindowHint(GLFW_SRGB_CAPABLE, GLFW_FALSE);
    glfwWindowHint(GLFW_DEPTH_BITS, 0);
    // A fullscreen window minimizes on focus loss by default; the stimulus must stay up while the
    // operator works in the console on another monitor.
    glfwWindowHint(GLFW_AUTO_ICONIFY, GLFW_FALSE);

    GLFWwindow* window = glfwCreateWindow(1920, 540, "Chronolume stimulus", nullptr, nullptr);
    if (!window) {
        std::fprintf(stderr, "glfwCreateWindow failed\n");
        glfwTerminate();
        return 1;
    }
    glfwMakeContextCurrent(window);
    glfwSwapInterval(1);
#ifdef __APPLE__
    chronolume_set_device_rgb_color_space(window);
#endif

    GLuint tex = 0;
    glGenTextures(1, &tex);
    GLuint image_tex = 0;
    glGenTextures(1, &image_tex);
    app.image_tex = image_tex;
    glBindTexture(GL_TEXTURE_2D, tex);
    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();
    update_title(window, app);

    while (!glfwWindowShouldClose(window)) {
        glfwPollEvents();
        if (key_pressed(window, GLFW_KEY_ESCAPE)) {
            break;
        }

        const int ladder_keys[] = {GLFW_KEY_0, GLFW_KEY_1, GLFW_KEY_2, GLFW_KEY_3,
                                   GLFW_KEY_4, GLFW_KEY_5, GLFW_KEY_6};
        for (int i = 0; i < 7; ++i) {
            if (key_pressed(window, ladder_keys[i])) {
                stimulus_set_blue_level(&app.stimulus, kBlueLadder[i]);
                app.dirty = true;
            }
        }
        if (key_pressed(window, GLFW_KEY_K)) {
            stimulus_set_rgb(&app.stimulus, 0, 0, 0, "black");
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_R)) {
            stimulus_set_rgb(&app.stimulus, 255, 0, 0, "red");
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_G)) {
            stimulus_set_rgb(&app.stimulus, 0, 255, 0, "green");
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_B)) {
            stimulus_set_rgb(&app.stimulus, 0, 0, 255, "blue");
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_W)) {
            stimulus_set_rgb(&app.stimulus, 255, 255, 255, "white");
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_F1)) {
            app.stimulus.eye = EyeTarget::Left;
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_F2)) {
            app.stimulus.eye = EyeTarget::Right;
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_F3)) {
            app.stimulus.eye = EyeTarget::Both;
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_LEFT_BRACKET)) {
            app.device->set_brightness(app.device->info().brightness - 1);
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_RIGHT_BRACKET)) {
            app.device->set_brightness(app.device->info().brightness + 1);
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_COMMA)) {
            set_duty_manual(&app, app.device->info().duty_cycle - 10);
        }
        if (key_pressed(window, GLFW_KEY_PERIOD)) {
            set_duty_manual(&app, app.device->info().duty_cycle + 10);
        }
        if (key_pressed(window, GLFW_KEY_9)) {
            set_duty_manual(&app, 98);
        }
        if (key_pressed(window, GLFW_KEY_F)) {
            const float film = app.device->info().film;
            app.device->set_film(film > 0.0f ? 0.0f : 1.0f);
            app.dirty = true;
        }
        if (key_pressed(window, GLFW_KEY_S)) {
            if (app.log.active()) {
                const std::string p = app.log.path();
                app.log.stop();
                std::printf("session stopped %s\n", p.c_str());
            } else {
                const std::string p = default_log_path();
                if (app.log.start(p)) {
                    const DeviceInfo info = app.device->info();
                    app.log.event("capability", {
                        {"backend", info.backend},
                        {"device", info.market_name},
                        {"firmware", info.firmware},
                        {"product_id", std::to_string(info.product_id)},
                    });
                    log_stimulus(&app);
                    std::printf("session started %s\n", p.c_str());
                } else {
                    std::fprintf(stderr, "could not write %s\n", p.c_str());
                }
            }
        }
        if (key_pressed(window, GLFW_KEY_D)) {
            print_info(app.device->info());
            if (app.log.active()) {
                app.log.event("capability_dump", stimulus_fields(app));
            }
        }
        if (key_pressed(window, GLFW_KEY_V)) {
            readback_halves(window);
            if (app.log.active()) {
                app.log.event("readback", stimulus_fields(app));
            }
        }
        if (key_pressed(window, GLFW_KEY_P)) {
            set_fullscreen(window, app, glfwGetWindowMonitor(window) == nullptr);
        }

        std::vector<CommandResult> results;
        bool quit = false;
        for (const std::string& line : app.control.poll()) {
            results.push_back(run_command(&app, window, line));
            quit = quit || results.back().quit;
        }

        const int wear = app.device->info().wear_status;
        if (wear != app.last_wear) {
            std::printf("wear %d\n", wear);
            if (app.log.active()) {
                std::map<std::string, std::string> fields = stimulus_fields(app);
                fields["previous_wear"] = std::to_string(app.last_wear);
                app.log.event("wear", fields);
            }
            app.last_wear = wear;
        }

        if (app.dirty) {
            log_stimulus(&app);
            update_title(window, app);
            app.dirty = false;
        }

        int fb_w = 0, fb_h = 0;
        glfwGetFramebufferSize(window, &fb_w, &fb_h);
        glViewport(0, 0, fb_w, fb_h);
        glClear(GL_COLOR_BUFFER_BIT);
        render_stimulus(app.stimulus, tex, app.image_tex);
        Readback rb;
        if (!results.empty()) {
            rb = read_back_buffer(window, app);
        }
        glfwSwapBuffers(window);
        ++app.frame;
        if (!results.empty()) {
            glFinish();
            for (const CommandResult& res : results) {
                app.control.send_line(command_response(app, res, rb));
            }
        }
        if (quit) {
            break;
        }
    }

    app.control.stop();
    app.log.stop();
    app.device->close();
    glfwDestroyWindow(window);
    glfwTerminate();
    return 0;
}
