#include "display/frame_image.hpp"

#include <cctype>
#include <cstdio>
#include <fstream>
#include <iterator>

namespace {

// Next whitespace-delimited header token, skipping '#' comments.
bool next_token(const std::vector<uint8_t>& buf, size_t* pos, std::string* tok) {
    tok->clear();
    while (*pos < buf.size()) {
        const char c = static_cast<char>(buf[*pos]);
        if (c == '#') {
            while (*pos < buf.size() && buf[*pos] != '\n') {
                ++*pos;
            }
        } else if (std::isspace(static_cast<unsigned char>(c))) {
            ++*pos;
        } else {
            break;
        }
    }
    while (*pos < buf.size() && !std::isspace(static_cast<unsigned char>(buf[*pos]))) {
        tok->push_back(static_cast<char>(buf[(*pos)++]));
    }
    return !tok->empty();
}

bool parse_dim(const std::string& s, int lo, int hi, int* out) {
    if (s.empty() || s.size() > 6) {
        return false;
    }
    int v = 0;
    for (char c : s) {
        if (c < '0' || c > '9') {
            return false;
        }
        v = v * 10 + (c - '0');
    }
    if (v < lo || v > hi) {
        return false;
    }
    *out = v;
    return true;
}

}  // namespace

uint64_t fnv1a64(const uint8_t* data, size_t n) {
    uint64_t h = 14695981039346656037ULL;
    for (size_t i = 0; i < n; ++i) {
        h ^= data[i];
        h *= 1099511628211ULL;
    }
    return h;
}

std::string hex64(uint64_t v) {
    char buf[17];
    std::snprintf(buf, sizeof(buf), "%016llx", static_cast<unsigned long long>(v));
    return buf;
}

bool load_ppm(const std::string& path, FrameImage* out, std::string* err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        *err = "cannot open " + path;
        return false;
    }
    const std::vector<uint8_t> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    std::string magic, ws, hs, ms;
    if (!next_token(buf, &pos, &magic) || magic != "P6") {
        *err = "not a binary PPM (P6)";
        return false;
    }
    int w = 0, h = 0, maxval = 0;
    if (!next_token(buf, &pos, &ws) || !next_token(buf, &pos, &hs) || !next_token(buf, &pos, &ms) ||
        !parse_dim(ws, 1, 16384, &w) || !parse_dim(hs, 1, 16384, &h) || !parse_dim(ms, 1, 65535, &maxval)) {
        *err = "bad PPM header";
        return false;
    }
    if (maxval != 255) {
        *err = "PPM maxval must be 255 (8-bit)";
        return false;
    }
    ++pos;  // the single whitespace byte after maxval
    const size_t n = static_cast<size_t>(w) * static_cast<size_t>(h) * 3;
    if (buf.size() < pos + n) {
        *err = "PPM pixel data truncated";
        return false;
    }
    out->w = w;
    out->h = h;
    out->rgb.assign(buf.begin() + static_cast<std::ptrdiff_t>(pos),
                    buf.begin() + static_cast<std::ptrdiff_t>(pos + n));
    out->path = path;
    out->hash = fnv1a64(out->rgb.data(), out->rgb.size());
    return true;
}
