#pragma once

#include <cstdint>
#include <string>

enum class EyeTarget { Left, Right, Both };

struct StimulusState {
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    EyeTarget eye = EyeTarget::Both;
    const char* label = "black";
    bool image = false;  // show the loaded frame image instead of the uniform color
};

void stimulus_set_rgb(StimulusState* s, uint8_t r, uint8_t g, uint8_t b, const char* label);
void stimulus_set_blue_level(StimulusState* s, uint8_t blue);
const char* eye_name(EyeTarget eye);
std::string rgb_string(const StimulusState& s);
