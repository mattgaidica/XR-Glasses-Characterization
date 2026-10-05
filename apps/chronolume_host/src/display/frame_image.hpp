#pragma once

#include <cstdint>
#include <string>
#include <vector>

// An 8-bit RGB frame loaded from a binary PPM (P6, maxval 255). Rows are stored top to bottom.
struct FrameImage {
    int w = 0;
    int h = 0;
    std::vector<uint8_t> rgb;
    std::string path;
    uint64_t hash = 0;  // FNV-1a 64 of the pixel bytes, top row first
};

bool load_ppm(const std::string& path, FrameImage* out, std::string* err);
uint64_t fnv1a64(const uint8_t* data, size_t n);
std::string hex64(uint64_t v);
