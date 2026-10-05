#include "display/stimulus.hpp"

#include <cstdio>

void stimulus_set_rgb(StimulusState* s, uint8_t r, uint8_t g, uint8_t b, const char* label) {
    s->r = r;
    s->g = g;
    s->b = b;
    s->label = label;
    s->image = false;
}

void stimulus_set_blue_level(StimulusState* s, uint8_t blue) {
    static char labels[8][32];
    static int i = 0;
    i = (i + 1) % 8;
    std::snprintf(labels[i], sizeof(labels[i]), "blue %u", static_cast<unsigned>(blue));
    stimulus_set_rgb(s, 0, 0, blue, labels[i]);
}

const char* eye_name(EyeTarget eye) {
    switch (eye) {
        case EyeTarget::Left:
            return "left";
        case EyeTarget::Right:
            return "right";
        case EyeTarget::Both:
            return "both";
    }
    return "both";
}

std::string rgb_string(const StimulusState& s) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "(%u,%u,%u)", s.r, s.g, s.b);
    return buf;
}
